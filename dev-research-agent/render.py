# render.py
"""
Closes the loop: takes create_video_assets()'s output, copies the voiceover
into the Remotion project's public/ folder, writes a props file Remotion's
CLI can consume, and renders the final MP4.
"""

import json
import shlex
import shutil
import subprocess
from pathlib import Path
from voiceover import clean_script_for_voiceover

REMOTION_PROJECT = Path("/Users/adityakumar/desktop/motion")
COMPOSITION_ID = "NarratedVideo"


def render_video(topic_result: dict) -> str:
    script_path = Path(topic_result["script_path"])
    slug = script_path.parent.name
    audio_filename = f"{slug}.wav"

    # 1. Copy voiceover into Remotion's public/ folder — it can't reach
    #    files outside its own project.
    dest = REMOTION_PROJECT / "public" / audio_filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(topic_result["audio_path"], dest)
    print(f"Copied voiceover to {dest}")

    # 2. Clean the script for on-screen text using the SAME function used
    #    for narration. Without this, the exact bug you caught earlier
    #    (markdown asterisks and stage directions read aloud) comes back
    #    as literal "**Hook:**" text visibly rendered on screen instead.
    raw_script = script_path.read_text()
    display_text = clean_script_for_voiceover(raw_script)

    # 3. Write a props file rather than inline --props JSON — inline JSON
    #    breaks on Windows shells and gets awkward to escape with long
    #    text either way; a file sidesteps both.
    props_path = REMOTION_PROJECT / f"{slug}-props.json"
    props_path.write_text(json.dumps({
        "audioFileName": audio_filename,
        "scriptText": display_text,
        "topic": topic_result.get("topic", slug.replace("-", " ").title()),
    }))

    # 4. Render
    out_path = script_path.resolve().parent / "video.mp4"
    print(f"Rendering video to {out_path}...")
    result = subprocess.run(
        ["npx", "remotion", "render", COMPOSITION_ID, str(out_path), f"--props={props_path}"],
        cwd=REMOTION_PROJECT,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("❌ Render failed:")
        print(result.stderr)
        raise RuntimeError("Remotion render failed")

    print(f"✅ Rendered {out_path}")
    return str(out_path)


def _run_remotion_render(composition_id: str, out_path: Path, props_path: Path) -> None:
    """Shared by the MCP-tool render entry points below (render_video()
    above still calls npx directly — untouched, still correct for its
    existing terminal-invoked call sites).

    `npx` here is installed via nvm, which only loads through .zprofile on
    a LOGIN shell — a bare subprocess env (what a GUI-launched MCP host
    like Claude Desktop gives this process) has no npx on PATH at all.
    Confirmed: `env -i which npx` fails, `zsh -lc 'which npx'` succeeds.
    Shelling out through `zsh -lc` sources .zprofile/nvm so this works
    regardless of the parent process's own PATH.
    """
    cmd = "npx remotion render {} {} --props={}".format(
        shlex.quote(composition_id),
        shlex.quote(str(out_path)),
        shlex.quote(str(props_path)),
    )
    result = subprocess.run(
        ["zsh", "-lc", cmd],
        cwd=REMOTION_PROJECT,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print("❌ Render failed:")
        print(result.stderr)
        raise RuntimeError("Remotion render failed")

    print(f"✅ Rendered {out_path}")


def render_from_script_and_audio(script_text: str, audio_path: str, slug: str | None = None) -> str:
    """MCP-tool entry point: same copy/props/render steps as render_video()
    above, but starting from raw script text + an audio file path instead of
    create_video_assets()'s output dict — there's no script_path on disk in
    this flow, since Claude drafts the script directly in conversation
    rather than agent.py writing it to output/<slug>/script.txt.

    Kept as a separate function (not a rewrite of render_video) so the
    existing batch pipeline entry point is untouched.
    """
    audio_src = Path(audio_path)
    slug = slug or audio_src.stem
    audio_filename = f"{slug}.wav"

    dest = REMOTION_PROJECT / "public" / audio_filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(audio_src, dest)
    print(f"Copied voiceover to {dest}")

    # Same cleaning function used for narration in voiceover.py — without
    # it, literal "**Hook:**" markdown would show up visibly on screen.
    display_text = clean_script_for_voiceover(script_text)

    props_path = REMOTION_PROJECT / f"{slug}-props.json"
    props_path.write_text(json.dumps({
        "audioFileName": audio_filename,
        "scriptText": display_text,
        "topic": slug.replace("-", " ").replace("_", " ").title(),
    }))

    # Absolute, rooted at this file's own directory — never the caller's
    # cwd, which an MCP host controls and won't be dev-research-agent/.
    out_dir = (Path(__file__).resolve().parent / "output" / slug)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "video.mp4"
    print(f"Rendering video to {out_path}...")

    _run_remotion_render(COMPOSITION_ID, out_path, props_path)
    return str(out_path)


def render_shots_to_video(
    shots: list[dict],
    audio_path: str,
    topic: str = "",
    slug: str | None = None,
) -> str:
    """MCP-tool entry point for structured screenplays: each shot carries
    its own narration text, an optional code snippet, and the
    screenwriter's intended relative timing (start/end in seconds — used
    as a pacing WEIGHT, not a literal clock time, since there's no
    word-level transcription alignment in this pipeline yet; see
    ScreenplayVideo.tsx for the full reasoning). Renders through the
    ScreenplayVideo composition instead of NarratedVideo.

    shots: list of {"text": str, "code": str | None, "language": str | None,
                     "start": float, "end": float}
    """
    audio_src = Path(audio_path)
    slug = slug or audio_src.stem
    audio_filename = f"{slug}.wav"

    dest = REMOTION_PROJECT / "public" / audio_filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(audio_src, dest)
    print(f"Copied voiceover to {dest}")

    # Same narration cleaning as every other entry point — strips markdown
    # so a shot's spoken line doesn't show literal "**bold**" on screen.
    # Code snippets aren't touched — they're a separate field, not
    # embedded in the narration text.
    cleaned_shots = [
        {**shot, "text": clean_script_for_voiceover(shot["text"])}
        for shot in shots
    ]

    props_path = REMOTION_PROJECT / f"{slug}-props.json"
    props_path.write_text(json.dumps({
        "audioFileName": audio_filename,
        "shots": cleaned_shots,
        "topic": topic or slug.replace("-", " ").replace("_", " ").title(),
    }))

    out_dir = Path(__file__).resolve().parent / "output" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "video.mp4"
    print(f"Rendering video to {out_path}...")

    _run_remotion_render("ScreenplayVideo", out_path, props_path)
    return str(out_path)