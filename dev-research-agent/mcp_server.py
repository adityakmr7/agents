# mcp_server.py
#
# Exposes the MECHANICAL steps of the video pipeline as MCP tools for
# Claude Desktop: web search, TTS, and Remotion rendering. The reasoning
# steps that agent.py's research -> draft -> critique -> revise -> fact-check
# chain used to farm out to a second LLM (Gemini/Ollama) are intentionally
# NOT ported here. That whole chain — provider fallback, tool-schema
# narrowing for weak models, the structured-output nudge/retry — exists to
# make an EXTERNAL model behave reliably inside an unattended script. None
# of that is needed when Claude Desktop is already the one in the
# conversation: it does research synthesis, drafting, critiquing, and
# revising directly, and only calls a tool for work that's genuinely just
# mechanical (an HTTP search call, a TTS render, a shell-out to Remotion).
#
# agent.py's run_pipeline() (the fully-automated batch path) is untouched —
# see CLAUDE.md for when to use which entry point.
#
# Every path here is resolved relative to PROJECT_ROOT (this file's own
# directory), never the process's cwd. Claude Desktop launches this server
# with its own working directory, not dev-research-agent/ — confirmed by
# reproducing the failure: importing agent.py from an arbitrary cwd raises
# a TAVILY_API_KEY validation error because agent.py's own `load_dotenv()`
# call can't find .env without a cwd hint. Pre-loading .env by absolute
# path below, before agent.py is imported, fixes that without touching
# agent.py itself.
#
# ASYNC JOBS: generate_voiceover/render_video/render_video_with_shots do
# real, slow work (Chatterbox TTS, npx remotion render) that regularly
# exceeds MCP clients' per-tool-call timeout — confirmed with a real call
# that took ~10 minutes end to end (mostly Chatterbox model loading, which
# happened on every single call with no caching) and errored client-side
# with "Request timed out" even though the server kept working and
# finished successfully in the background. No amount of "just wait" fixes
# a hard client-side timeout, so these tools now return a job_id
# IMMEDIATELY and do the real work on a background thread; poll
# check_job_status(job_id) for the result. The Chatterbox model is also
# now cached at module level (see _get_tts_model) so repeat calls within
# this server process's lifetime skip the slow reload entirely.

import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from chatterbox.tts import ChatterboxTTS  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

from agent import _tavily  # noqa: E402 — narrow Tavily wrapper, no agent/LLM wrapping it
from voiceover import generate_voiceover as _generate_voiceover  # noqa: E402
from render import render_from_script_and_audio, render_shots_to_video  # noqa: E402

VOICEOVER_DIR = PROJECT_ROOT / "output" / "mcp-voiceovers"

mcp = FastMCP("dev-video-pipeline")


# ─────────────────────────────────────────────────────────────
# TTS model cache — loading ChatterboxTTS.from_pretrained() involves HF
# Hub lookups + weight loading and is the dominant cost of a cold
# generate_voiceover call. This server process stays alive for the whole
# Claude Desktop session, so load once and reuse.
# ─────────────────────────────────────────────────────────────

_tts_model: ChatterboxTTS | None = None
_tts_model_lock = threading.Lock()


def _get_tts_model(device: str = "mps") -> ChatterboxTTS:
    global _tts_model
    if _tts_model is None:
        with _tts_model_lock:
            if _tts_model is None:  # re-check inside the lock
                print("Loading Chatterbox model (first call this session)...")
                _tts_model = ChatterboxTTS.from_pretrained(device=device)
    return _tts_model


# ─────────────────────────────────────────────────────────────
# Background jobs — every tool that does real work returns a job_id
# immediately instead of blocking. In-memory only: jobs don't survive a
# server restart, which is fine since Claude Desktop only restarts this
# process when it restarts itself, at which point any in-flight job's
# conversation context is gone too.
# ─────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _run_job(job_id: str, fn, args: tuple, kwargs: dict) -> None:
    try:
        result = fn(*args, **kwargs)
        _jobs[job_id] = {"status": "done", "result": result}
    except Exception as e:
        _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _start_job(fn, *args, **kwargs) -> str:
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"status": "running"}
    thread = threading.Thread(target=_run_job, args=(job_id, fn, args, kwargs), daemon=True)
    thread.start()
    return job_id


@mcp.tool()
def check_job_status(job_id: str) -> str:
    """Check progress of a background job started by generate_voiceover,
    render_video, or render_video_with_shots — those tools return a job_id
    immediately rather than blocking, since the real work (TTS synthesis,
    video rendering) routinely takes minutes, well past what a tool call
    can block for. Poll this every ~15-20s until it stops returning
    "running". Returns one of:
    - "running" — still working, poll again shortly.
    - "done: <path>" — finished; <path> is the result file (.wav or .mp4).
    - "error: <message>" — failed, with the reason.
    - "unknown job_id" — no job with that id (typo, or the server
      restarted since the job was started — jobs are in-memory only)."""
    job = _jobs.get(job_id)
    if job is None:
        return "unknown job_id"
    if job["status"] == "running":
        return "running"
    if job["status"] == "error":
        return f"error: {job['error']}"
    return f"done: {job['result']}"


@mcp.tool()
def search_dev_topic(query: str) -> str:
    """Search the web for current, accurate information about a technical dev
    topic. Returns raw results (title, URL, snippet) for you to read and
    synthesize directly in conversation — this tool does no summarization
    or reasoning of its own, it's a plain search call. Fast — no job/polling
    needed."""
    raw = _tavily.invoke({"query": query})
    results = raw.get("results", []) if isinstance(raw, dict) else []
    if not results:
        return f"No results found for: {query}"

    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        lines.append(f"{i}. {title}\n   {url}\n   {content}")
    return "\n\n".join(lines)


@mcp.tool()
def generate_voiceover(script_text: str, reference_path: str = "aditya-voice.m4a") -> str:
    """Start generating a cloned-voice narration voiceover from FINAL script
    text, using Chatterbox TTS on MPS. Pass the script text you've already
    drafted and finalized in conversation — this tool only strips markdown
    and stage directions before synthesis (clean_script_for_voiceover), it
    does no drafting or editing. reference_path is resolved relative to
    the project root if not already absolute.

    Runs in the BACKGROUND — returns a job_id immediately (generation
    regularly takes minutes, especially the first call in a session while
    the model loads). Poll check_job_status(job_id); when done, the result
    is the path to the generated .wav file."""
    ref = Path(reference_path)
    if not ref.is_absolute():
        ref = PROJECT_ROOT / ref
    if not ref.exists():
        raise FileNotFoundError(
            f"Reference voice clip not found: {ref}. Pass an absolute path, "
            f"or place the clip at {PROJECT_ROOT} as '{reference_path}'."
        )

    VOICEOVER_DIR.mkdir(parents=True, exist_ok=True)
    output_path = VOICEOVER_DIR / f"voiceover-{int(time.time())}.wav"

    def _do_generate() -> str:
        # _get_tts_model() must be called INSIDE the background thread —
        # calling it here as a `model=_get_tts_model()` kwarg would
        # evaluate eagerly in the dispatching thread (Python evaluates
        # call arguments before the call happens), blocking this tool
        # for the full model-load time and defeating the entire point of
        # _start_job. Confirmed this exact mistake with a real timed run
        # (18.7s to "return immediately") before catching it here.
        return _generate_voiceover(
            script_text,
            reference_path=str(ref),
            output_path=str(output_path),
            model=_get_tts_model(),
        )

    return _start_job(_do_generate)


@mcp.tool()
def render_video(script_text: str, audio_path: str) -> str:
    """Start rendering the final vertical (1080x1920) video from FINAL
    narration script text and an already-generated voiceover .wav path
    (typically generate_voiceover's job result). Copies the audio into the
    Remotion project's public/ folder, writes the on-screen caption text
    (same markdown/stage-direction stripping as generate_voiceover), and
    shells out to `npx remotion render`.

    Runs in the BACKGROUND — returns a job_id immediately (rendering is
    the slow step). Poll check_job_status(job_id); when done, the result
    is the path to the rendered .mp4. Confirm the script and voiceover
    sound right before calling this — not worth rendering output that's
    about to be redone."""
    return _start_job(render_from_script_and_audio, script_text, audio_path)


class Shot(BaseModel):
    """One beat of a structured screenplay (hook, a method explained, a
    code-driven point, a CTA, etc.) — narration text plus an optional code
    snippet to overlay."""

    text: str = Field(description="Spoken narration line for this shot (also shown on screen).")
    code: str | None = Field(default=None, description="Optional code snippet to overlay during this shot.")
    language: str = Field(default="js", description="Label shown on the code card, e.g. 'js', 'py', 'ts'.")
    start: float = Field(description="Screenplay-authored start time in seconds (used as a relative pacing weight, not a literal clock time — see render_video_with_shots).")
    end: float = Field(description="Screenplay-authored end time in seconds (same caveat as start).")


@mcp.tool()
def render_video_with_shots(shots: list[Shot], audio_path: str, topic: str = "") -> str:
    """Start rendering a vertical (1080x1920) video from a STRUCTURED
    screenplay instead of flat script text — use this over render_video
    when the content has distinct beats with their own code overlays (e.g.
    a talking-points reel: hook, method 1 + snippet, method 2 + snippet,
    trap + snippet, CTA), matching a shot list like:
    [{"text": "...", "code": "...", "language": "js", "start": 0, "end": 8}, ...]

    Each shot's (start, end) sets its RELATIVE pacing weight, not a literal
    clock time — there's no word-level transcription alignment in this
    pipeline, so a hand-authored timestamp won't match the actual TTS
    audio's pacing exactly. The shots' relative durations (a short hook,
    a longer explainer beat) ARE preserved and rescaled to the real
    generated audio's length.

    audio_path should come from generate_voiceover's job result, called on
    the shots' concatenated narration text in order. Renders through the
    ScreenplayVideo composition (code-overlay-aware) rather than
    NarratedVideo.

    Runs in the BACKGROUND — returns a job_id immediately. Poll
    check_job_status(job_id); when done, the result is the path to the
    rendered .mp4. Confirm the voiceover sounds right before calling this."""
    return _start_job(
        render_shots_to_video,
        [shot.model_dump() for shot in shots],
        audio_path,
        topic=topic,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
