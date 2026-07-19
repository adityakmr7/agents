# main.py
"""
End-to-end pipeline: topic -> researched/critiqued/revised script -> cloned voiceover.

Orchestrates agent.py (script generation) and voiceover.py (TTS) into a
single entry point. Output: a final script (.txt) and a voiceover (.wav),
saved per-topic so repeat runs don't overwrite each other — both ready to
hand to Remotion.

With --instagram: also runs the AssetHunterAgent (Pexels fetch) and
VideoComposerAgent (InstagramReel render) to produce a 9:16 reel.mp4.
"""

import argparse
import re
import sys
from pathlib import Path
from render import render_video
from agent import run_pipeline
from voiceover import generate_voiceover
from tracker import RunTracker

def slugify(text: str) -> str:
    """Turn a topic into a safe folder/filename fragment."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:50]


def create_video_assets(topic: str, reference_path: str = "aditya-voice.m4a") -> dict:
    slug = slugify(topic)
    out_dir = Path(f"output/{slug}")
    out_dir.mkdir(parents=True, exist_ok=True)
    tracker = RunTracker()

    print(f"=== [1/2] Generating script for: {topic} ===")
    script = run_pipeline(topic,tracker=tracker)

    script_path = out_dir / "script.txt"
    script_path.write_text(script)
    print(f"Saved script to {script_path}")

    print("=== [2/2] Generating voiceover ===")
    audio_path = out_dir / "voiceover.wav"
    generate_voiceover(script, reference_path=reference_path, output_path=str(audio_path))
    tracker.print_summary()
    tracker.save(out_dir / "run_log.json")
    return {
        "topic": topic,
        "script_path": str(script_path),
        "audio_path": str(audio_path),
        "script": script,
        "slug": slug,
    }


def run_instagram_pipeline(result: dict) -> str:
    """
    Stage 3 (optional, --instagram flag): asset fetch + reel render.

    Takes the output dict from create_video_assets() and runs:
      1. AssetHunterAgent  — LLM extracts keywords, Pexels downloads assets.
      2. VideoComposerAgent — merges assets + shots, renders InstagramReel.

    Returns the path to the rendered reel.mp4.
    """
    # Lazy imports so the heavy asset_hunter deps don't load on every run
    from asset_hunter import run_asset_pipeline
    from video_composer import compose_instagram_video

    slug = result["slug"]
    script = result["script"]
    audio_path = result["audio_path"]

    print("\n=== [3/3] Running Instagram Reel Pipeline ===")

    print("[3a/3] Hunting visual assets (Pexels)...")
    asset_manifest = run_asset_pipeline(script, slug)

    print("[3b/3] Composing and rendering Instagram Reel...")
    reel_path = compose_instagram_video(
        shots=None,          # auto-split from script text
        asset_manifest=asset_manifest,
        audio_path=audio_path,
        topic=result["topic"],
        slug=slug,
        script_text=script,
    )

    return reel_path


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="videogen",
        description="Topic -> researched script -> cloned voiceover -> rendered vertical video.",
    )
    parser.add_argument("topic", help="Video topic, e.g. \"React Navigation vs Expo Router\"")
    parser.add_argument(
        "--voice",
        default="aditya-voice.m4a",
        help="Reference clip to clone the voice from (default: aditya-voice.m4a)",
    )
    parser.add_argument(
        "--no-render",
        action="store_true",
        help="Stop after script + voiceover — skip the Remotion render (the slow step)",
    )
    parser.add_argument(
        "--instagram",
        action="store_true",
        help="After script + voiceover, also fetch visual assets from Pexels and render a 9:16 Instagram Reel",
    )
    args = parser.parse_args()

    result = create_video_assets(args.topic, reference_path=args.voice)
    print(f"\nScript:    {result['script_path']}")
    print(f"Voiceover: {result['audio_path']}")

    if args.no_render:
        print("\n--no-render set, skipping video render.")
        return

    if args.instagram:
        reel_path = run_instagram_pipeline(result)
        print(f"\n=== DONE ===\nReel: {reel_path}")
        return

    video_path = render_video(result)
    print(f"\n=== DONE ===\nVideo: {video_path}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)