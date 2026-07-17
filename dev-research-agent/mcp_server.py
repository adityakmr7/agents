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

import time
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(PROJECT_ROOT / ".env")

from mcp.server.fastmcp import FastMCP  # noqa: E402

from agent import _tavily  # noqa: E402 — narrow Tavily wrapper, no agent/LLM wrapping it
from voiceover import generate_voiceover as _generate_voiceover  # noqa: E402
from render import render_from_script_and_audio  # noqa: E402

VOICEOVER_DIR = PROJECT_ROOT / "output" / "mcp-voiceovers"

mcp = FastMCP("dev-video-pipeline")


@mcp.tool()
def search_dev_topic(query: str) -> str:
    """Search the web for current, accurate information about a technical dev
    topic. Returns raw results (title, URL, snippet) for you to read and
    synthesize directly in conversation — this tool does no summarization
    or reasoning of its own, it's a plain search call."""
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
def generate_voiceover(script_text: str, reference_path: str = "my_voice.wav") -> str:
    """Generate a cloned-voice narration voiceover from FINAL script text,
    using Chatterbox TTS on MPS. Pass the script text you've already
    drafted and finalized in conversation — this tool only strips markdown
    and stage directions before synthesis (clean_script_for_voiceover), it
    does no drafting or editing. reference_path is resolved relative to
    the project root if not already absolute. Returns the path to the
    generated .wav file."""
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

    return _generate_voiceover(
        script_text,
        reference_path=str(ref),
        output_path=str(output_path),
    )


@mcp.tool()
def render_video(script_text: str, audio_path: str) -> str:
    """Render the final vertical (1080x1920) video from FINAL narration
    script text and an already-generated voiceover .wav path (typically
    generate_voiceover's return value). Copies the audio into the Remotion
    project's public/ folder, writes the on-screen caption text (same
    markdown/stage-direction stripping as generate_voiceover), and shells
    out to `npx remotion render`. Returns the path to the rendered .mp4.
    This is the slow step — confirm the script and voiceover sound right
    before calling it, rendering isn't worth doing on output about to be
    redone."""
    return render_from_script_and_audio(script_text, audio_path)


if __name__ == "__main__":
    mcp.run(transport="stdio")
