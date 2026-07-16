# main.py
"""
End-to-end pipeline: topic -> researched/critiqued/revised script -> cloned voiceover.

Orchestrates agent.py (script generation) and voiceover.py (TTS) into a
single entry point. Output: a final script (.txt) and a voiceover (.wav),
saved per-topic so repeat runs don't overwrite each other — both ready to
hand to Remotion.
"""

import re
from pathlib import Path

from agent import run_pipeline
from voiceover import generate_voiceover
from tracker import RunTracker

def slugify(text: str) -> str:
    """Turn a topic into a safe folder/filename fragment."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:50]


def create_video_assets(topic: str, reference_path: str = "osho-voice.mp3") -> dict:
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
    }


if __name__ == "__main__":
    result = create_video_assets("React's useTransition hook")
    print("\n=== DONE ===")
    print(f"Script:    {result['script_path']}")
    print(f"Voiceover: {result['audio_path']}")