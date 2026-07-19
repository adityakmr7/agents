# video_composer.py
#
# VideoComposerAgent — bridges the asset manifest from asset_hunter.py and
# the Remotion render for the new InstagramReel composition.
#
# Responsibilities:
#   1. Merge shot list (from agent.py's script) with the asset manifest
#      (from asset_hunter.py) so each shot knows which background file to show.
#   2. Copy all local asset files into Remotion's public/assets/<slug>/ folder
#      so Remotion can resolve them by static URL.
#   3. Write reel-props.json for the InstagramReel composition.
#   4. Shell out to `npx remotion render InstagramReel` via the same
#      _run_remotion_render helper used by the rest of the render pipeline.
#
# The "shot list" expected here is a list of dicts with at minimum:
#   {"text": str, "start": float, "end": float}
# with optional "code", "language" fields (same schema as ScreenplayShot).
# If no shot list is provided (e.g. flat-script mode), the script text is
# split sentence-by-sentence and equal timing weights are assigned.

import json
import re
import shutil
from pathlib import Path

from voiceover import clean_script_for_voiceover

# Remotion project root — same constant as render.py
REMOTION_PROJECT = Path("/Users/adityakumar/desktop/motion")
COMPOSITION_ID = "InstagramReel"


# ─────────────────────────────────────────────────────────────
# SHOT HELPERS
# ─────────────────────────────────────────────────────────────

WORDS_PER_SECOND = 2.5  # rough average TTS speaking pace, used to weight shots by content
MIN_SHOT_SECONDS = 1.5

def _sentences_to_shots(script_text: str) -> list[dict]:
    """
    Fallback: if no explicit shot list is provided, split the clean narration
    text into sentences and weight each by estimated spoken duration (word
    count / WORDS_PER_SECOND), not a flat 1.0 for every sentence regardless
    of length. The flat-weight version gave a 4-word CTA the same screen
    time as a 30-word explanation — wrong on its own, and it also meant
    downstream beat-count computation (asset_hunter.run_beat_pipeline_for_shots)
    had no real per-shot duration signal to work from. Rendering still
    rescales these against the actual TTS audio length (same "relative
    weight, not literal seconds" approach as every other shot list in this
    project), so this only needs to be a reasonable estimate.
    """
    narration = clean_script_for_voiceover(script_text)
    sentences = re.split(r"(?<=[.!?])\s+", narration.strip())
    shots = []
    cursor = 0.0
    for sentence in sentences:
        text = sentence.strip()
        if not text:
            continue
        duration = max(len(text.split()) / WORDS_PER_SECOND, MIN_SHOT_SECONDS)
        shots.append({"text": text, "start": cursor, "end": cursor + duration})
        cursor += duration
    return shots


def _merge_shots_and_assets(
    shots: list[dict],
    asset_manifest: list[dict],
) -> list[dict]:
    """
    Merge the asset manifest into the shot list by matching shot_index.
    Each shot gets an "assetFile" key (Remotion-relative path, e.g.
    "assets/<slug>/shot-00-developer-typing.mp4") and "assetType".

    Shots with no matched asset get assetFile=None — InstagramReel.tsx
    renders a solid dark gradient fallback for those.
    """
    # Build a lookup by shot_index
    asset_by_shot: dict[int, dict] = {
        a["shot_index"]: a for a in asset_manifest if a.get("local_path")
    }

    merged = []
    for i, shot in enumerate(shots):
        asset = asset_by_shot.get(i)
        merged.append({
            **shot,
            "text": clean_script_for_voiceover(shot.get("text", "")),
            "assetFile": asset["remotion_path"] if asset else None,
            "assetType": asset["asset_type"] if asset else "placeholder",
            "credit": asset.get("credit") if asset else None,
        })

    return merged


# ─────────────────────────────────────────────────────────────
# ASSET STAGING (copy into Remotion's public/ folder)
# ─────────────────────────────────────────────────────────────

def _stage_assets(asset_manifest: list[dict], slug: str) -> list[dict]:
    """
    Copy each downloaded asset file into Remotion's public/assets/<slug>/ folder.
    Adds a "remotion_path" key to each manifest item (the relative URL Remotion
    uses to look up static files, e.g. "assets/my-topic/shot-00-code.mp4").

    Returns the updated manifest with remotion_path filled in.
    """
    remotion_assets_dir = REMOTION_PROJECT / "public" / "assets" / slug
    remotion_assets_dir.mkdir(parents=True, exist_ok=True)

    updated = []
    for item in asset_manifest:
        local_path = item.get("local_path")
        if not local_path or not Path(local_path).exists():
            updated.append({**item, "remotion_path": None})
            continue

        src = Path(local_path)
        dest = remotion_assets_dir / src.name
        shutil.copy2(src, dest)

        # Remotion staticFile() resolves relative to the public/ folder
        remotion_path = f"assets/{slug}/{src.name}"
        updated.append({**item, "remotion_path": remotion_path})

    return updated


# ─────────────────────────────────────────────────────────────
# BEAT STAGING + MERGING — for asset_hunter.run_beat_pipeline_for_shots()
#
# Parallel to _stage_assets()/_merge_shots_and_assets() above (untouched,
# still correct for the single-asset-per-shot path), but each shot now
# carries a "beats" list — several distinct visuals per shot instead of
# one — see asset_hunter.py's beat-extraction comment for why.
# ─────────────────────────────────────────────────────────────

def _stage_beats(beat_manifest: list[dict], slug: str) -> list[dict]:
    """Beat-level counterpart to _stage_assets() — copies each beat's
    local_path (photo beats only; stat/icons beats have none) into
    Remotion's public/assets/<slug>/ folder and adds "remotion_path"."""
    remotion_assets_dir = REMOTION_PROJECT / "public" / "assets" / slug
    remotion_assets_dir.mkdir(parents=True, exist_ok=True)

    staged = []
    for item in beat_manifest:
        local_path = item.get("local_path")
        if not local_path or not Path(local_path).exists():
            staged.append({**item, "remotion_path": None})
            continue
        src = Path(local_path)
        dest = remotion_assets_dir / src.name
        shutil.copy2(src, dest)
        staged.append({**item, "remotion_path": f"assets/{slug}/{src.name}"})
    return staged


def _merge_shots_and_beats(shots: list[dict], beat_manifest: list[dict]) -> list[dict]:
    """Groups the flat beat manifest by shot_index and attaches a `beats`
    list to each shot — InstagramReel.tsx's beat-aware schema, replacing
    the old single assetFile/assetType/credit per shot."""
    beats_by_shot: dict[int, list[dict]] = {}
    for b in beat_manifest:
        beats_by_shot.setdefault(b["shot_index"], []).append(b)

    merged = []
    for i, shot in enumerate(shots):
        shot_beats = sorted(beats_by_shot.get(i, []), key=lambda b: b["beat_index"])
        beats_out = []
        for b in shot_beats:
            if b["type"] == "photo":
                beats_out.append({
                    "type": "photo",
                    "assetFile": b.get("remotion_path"),
                    "credit": b.get("credit"),
                })
            elif b["type"] == "stat":
                beats_out.append({"type": "stat", "text": b.get("text")})
            elif b["type"] == "icons":
                beats_out.append({"type": "icons", "concept": b.get("concept")})
        if not beats_out:
            # No beats resolved for this shot at all (extraction failure) —
            # a single empty-asset photo beat renders InstagramReel's
            # existing dark-gradient fallback rather than an empty frame.
            beats_out = [{"type": "photo", "assetFile": None, "credit": None}]
        merged.append({
            **shot,
            "text": clean_script_for_voiceover(shot.get("text", "")),
            "beats": beats_out,
        })
    return merged


def compose_instagram_reel_from_beats(
    shots: list[dict],
    beat_manifest: list[dict],
    audio_path: str,
    topic: str = "",
    slug: str | None = None,
) -> str:
    """Beat-aware counterpart to compose_instagram_video() — same
    voiceover-copy/props/render steps, but stages+merges per-beat assets
    (_stage_beats/_merge_shots_and_beats) instead of one asset per shot.
    Kept separate from compose_instagram_video() rather than rewriting it
    in place, so the single-asset path (asset_hunter.run_asset_pipeline*)
    still works unchanged for any caller still using it."""
    from render import _run_remotion_render  # noqa: PLC0415 — avoids circular import at module load

    audio_src = Path(audio_path)
    slug = slug or audio_src.stem
    audio_filename = f"{slug}.wav"

    print(f"[VideoComposer] Composing beat-based reel for '{topic or slug}'...")

    staged = _stage_beats(beat_manifest, slug)

    audio_dest = REMOTION_PROJECT / "public" / audio_filename
    audio_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(audio_src, audio_dest)
    print(f"[VideoComposer] Copied voiceover → {audio_dest}")

    shots_with_beats = _merge_shots_and_beats(shots, staged)

    props = _build_reel_props(shots_with_beats, audio_filename, topic, slug)
    props_path = REMOTION_PROJECT / f"{slug}-reel-props.json"
    props_path.write_text(json.dumps(props, indent=2))
    print(f"[VideoComposer] Props written → {props_path}")

    out_dir = Path(__file__).resolve().parent / "output" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "reel.mp4"
    print(f"[VideoComposer] Starting Remotion render → {out_path}")

    _run_remotion_render(COMPOSITION_ID, out_path, props_path)

    print(f"[VideoComposer] ✅ Reel rendered: {out_path}")
    return str(out_path)


# ─────────────────────────────────────────────────────────────
# PROPS BUILDER
# ─────────────────────────────────────────────────────────────

def _build_reel_props(
    shots_with_assets: list[dict],
    audio_filename: str,
    topic: str,
    slug: str,
) -> dict:
    """Build the props dict that InstagramReel.tsx expects."""
    return {
        "audioFileName": audio_filename,
        "shots": shots_with_assets,
        "topic": topic or slug.replace("-", " ").replace("_", " ").title(),
    }


# ─────────────────────────────────────────────────────────────
# MAIN COMPOSE FUNCTION
# ─────────────────────────────────────────────────────────────

def compose_instagram_video(
    shots: list[dict] | None,
    asset_manifest: list[dict],
    audio_path: str,
    topic: str = "",
    slug: str | None = None,
    script_text: str = "",
) -> str:
    """
    Compose and render an Instagram Reel.

    Args:
        shots:          List of shot dicts ({text, start, end, code?, language?}).
                        If None, script_text is split sentence-by-sentence.
        asset_manifest: Output of asset_hunter.run_asset_pipeline().
        audio_path:     Absolute path to the generated voiceover .wav file.
        topic:          Human-readable topic title (shown on screen).
        slug:           Short safe folder name; derived from audio filename if omitted.
        script_text:    Full script text — only used when shots is None.

    Returns:
        Absolute path to the rendered reel.mp4.
    """
    # Lazy import to avoid circular imports — render.py imports voiceover.py
    # which agent.py uses, and asset_hunter.py imports agent.py.
    from render import _run_remotion_render  # noqa: PLC0415

    audio_src = Path(audio_path)
    slug = slug or audio_src.stem
    audio_filename = f"{slug}.wav"

    print(f"[VideoComposer] Composing reel for '{topic or slug}'...")

    # 1. Resolve shot list
    if not shots:
        print("[VideoComposer] No shots provided — splitting script into sentences.")
        shots = _sentences_to_shots(script_text)

    # 2. Stage assets (copy → Remotion's public/)
    print(f"[VideoComposer] Staging {len(asset_manifest)} assets into Remotion public/...")
    staged_manifest = _stage_assets(asset_manifest, slug)

    # 3. Copy voiceover into Remotion's public/
    audio_dest = REMOTION_PROJECT / "public" / audio_filename
    audio_dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(audio_src, audio_dest)
    print(f"[VideoComposer] Copied voiceover → {audio_dest}")

    # 4. Merge shots + assets
    shots_with_assets = _merge_shots_and_assets(shots, staged_manifest)

    # 5. Write props file
    props = _build_reel_props(shots_with_assets, audio_filename, topic, slug)
    props_path = REMOTION_PROJECT / f"{slug}-reel-props.json"
    props_path.write_text(json.dumps(props, indent=2))
    print(f"[VideoComposer] Props written → {props_path}")

    # 6. Render
    out_dir = Path(__file__).resolve().parent / "output" / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "reel.mp4"
    print(f"[VideoComposer] Starting Remotion render → {out_path}")

    _run_remotion_render(COMPOSITION_ID, out_path, props_path)

    print(f"[VideoComposer] ✅ Reel rendered: {out_path}")
    return str(out_path)


# ─────────────────────────────────────────────────────────────
# STANDALONE ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 3:
        print("Usage: python video_composer.py <slug> <audio_path>")
        print("  Reads output/<slug>/script.txt and output/<slug>/asset_manifest.json")
        sys.exit(1)

    _slug = sys.argv[1]
    _audio = sys.argv[2]

    _script_path = Path(f"output/{_slug}/script.txt")
    _manifest_path = Path(f"output/{_slug}/asset_manifest.json")

    if not _script_path.exists():
        print(f"❌ No script at {_script_path}")
        sys.exit(1)

    _script = _script_path.read_text()
    _manifest = json.loads(_manifest_path.read_text()) if _manifest_path.exists() else []

    _reel_path = compose_instagram_video(
        shots=None,
        asset_manifest=_manifest,
        audio_path=_audio,
        topic=_slug.replace("-", " ").title(),
        slug=_slug,
        script_text=_script,
    )
    print(f"\n=== REEL OUTPUT ===\n{_reel_path}")
