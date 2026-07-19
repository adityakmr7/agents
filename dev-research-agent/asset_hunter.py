# asset_hunter.py
#
# AssetHunterAgent — two-stage pipeline:
#   1. LLM agent reads the final script and outputs one visual keyword
#      query per shot (JSON).
#   2. Pexels API fetches the best matching image or short video clip per
#      query and downloads it to output/<slug>/assets/.
#
# Pexels is free, CC0-licensed for commercial use, and needs no attribution
# for video. A free API key (PEXELS_API_KEY in .env) is required.
# Get one at https://www.pexels.com/api/
#
# The asset manifest returned by run_asset_pipeline() is a list of dicts:
#   {
#     "shot_index": int,
#     "keyword": str,
#     "asset_type": "photo" | "video",
#     "local_path": str,      # absolute path to downloaded file
#     "pexels_url": str,      # original Pexels page URL
#     "credit": str,          # photographer/videographer name for attribution
#   }
# This is passed directly to video_composer.py and render.py.

import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "")
PEXELS_PHOTO_URL = "https://api.pexels.com/v1/search"
PEXELS_VIDEO_URL = "https://api.pexels.com/videos/search"


# ─────────────────────────────────────────────────────────────
# KEYWORD EXTRACTION — LLM AGENT
# ─────────────────────────────────────────────────────────────

# Import the same model factory and fallback wrapper already in agent.py
# so the asset analyst uses the same Gemini→Ollama fallback pattern as
# every other agent in this pipeline.
from agent import invoke_with_fallback, get_model_for
from langchain.agents import create_agent
from langchain_core.tools import tool


ASSET_ANALYST_SYSTEM_PROMPT = """
You are a visual asset coordinator for short-form Instagram Reels about developer topics.

Given a video script, break it into individual "shots" (one per distinct point or sentence group)
and for EACH shot produce ONE short Pexels search keyword that would find a visually relevant
background image or video.

Rules:
- Keep each keyword under 4 words — shorter queries get better Pexels results.
- For coding/technical points, prefer concrete visuals: "developer typing code", "laptop dark screen".
- For conceptual points, prefer relatable visuals: "fast loading app", "smooth animation".
- Never use the word "React" or framework names as keywords — Pexels has no relevant results for those.
- asset_type must be either "photo" or "video".

Respond ONLY with a valid JSON array. No markdown, no preamble, no trailing text. Example:
[
  {"shot_index": 0, "keyword": "developer typing code", "asset_type": "video"},
  {"shot_index": 1, "keyword": "smooth app animation", "asset_type": "video"},
  {"shot_index": 2, "keyword": "laptop dark screen", "asset_type": "photo"}
]
""".strip()


def make_asset_analyst(model):
    """Factory: returns an asset-analyst agent bound to the given model."""
    return create_agent(
        model=model,
        tools=[],
        system_prompt=ASSET_ANALYST_SYSTEM_PROMPT,
    )


def extract_shot_queries(script: str) -> list[dict]:
    """
    Run the AssetAnalystAgent on the script.
    Returns a list of {shot_index, keyword, asset_type} dicts.

    Falls back from Gemini to Ollama on any failure. If the LLM returns
    malformed JSON (rare but possible), we log the raw output and return
    an empty list rather than crashing the whole pipeline — the composer
    handles missing assets gracefully with a solid-color fallback.
    """
    print("[AssetHunter] Extracting visual keywords from script...")
    raw = invoke_with_fallback(
        make_asset_analyst,
        f"Here is the script:\n\n{script}",
        step_name="asset_analyst",
    )

    # Strip fenced code block markers if the model wrapped the JSON in ```
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)

    try:
        queries = json.loads(clean)
        print(f"[AssetHunter] Got {len(queries)} shot queries.")
        return queries
    except json.JSONDecodeError as e:
        print(f"[AssetHunter] ⚠️  JSON parse failed ({e}). Raw output:\n{raw}")
        return []


# ─────────────────────────────────────────────────────────────
# KEYWORD EXTRACTION FOR A PRE-SEGMENTED SHOT LIST
#
# extract_shot_queries() above asks the model to invent its OWN shot
# boundaries and return its own shot_index numbering. That's fine as long
# as exactly that same segmentation is used for rendering — but the
# webapp's reel mode segments shots deterministically in Python
# (video_composer._sentences_to_shots) BEFORE asking for keywords. If the
# model's own segmentation doesn't match 1:1 (e.g. it merges two short
# sentences into one "shot" where the sentence-splitter kept them
# separate), the returned shot_index values silently point at the wrong
# shots once merged — an asset meant for shot 3 attaches to shot 2's
# text, with no error, just wrong-looking output.
#
# These functions sidestep that class of bug entirely: the shot list is
# already fixed before calling the model, one call is made PER shot (not
# one call returning a whole indexed array), and the shot_index in the
# returned manifest is assigned by the Python loop itself — never parsed
# from model output. Alignment is guaranteed by construction, not by the
# model correctly echoing indices back.
#
# Uses primary="ollama" (not the module default "gemini") — this is a
# short, low-stakes "give me a 2-4 word search term" task per shot, and
# Gemini's free tier is 20 requests/day; a 6-shot reel would burn a third
# of the daily quota on keyword extraction alone if it defaulted to
# Gemini-primary like the main script pipeline does.
# ─────────────────────────────────────────────────────────────

KEYWORD_SYSTEM_PROMPT = """
You are a visual asset coordinator for short-form Instagram Reels about developer topics.

Given ONE line of narration, respond with a single short Pexels search query (2-4 words)
that would find a visually relevant background photo for it.

Rules:
- Under 4 words.
- Prefer concrete visuals over abstract ones: "developer typing code", "laptop dark screen".
- Never use a framework/product name (React, Python, Cursor, etc.) as the query — Pexels has
  no relevant results for those; describe the VISUAL instead.
- Respond with ONLY the search query itself. No quotes, no punctuation, no explanation.
""".strip()


def make_keyword_agent(model):
    return create_agent(model=model, tools=[], system_prompt=KEYWORD_SYSTEM_PROMPT)


def extract_keyword_for_shot(shot_text: str) -> str:
    # ollama -> groq -> gemini: Gemini goes LAST now, not second — it's the
    # most quota-constrained of the three (20 free requests/day) and the
    # only one that returned a real 503 (server overload) in production.
    # Groq is free, cloud-hosted (no local app to keep running, unlike
    # Ollama), and generally reliable — a genuine middle tier, not padding.
    raw = invoke_with_fallback(
        make_keyword_agent,
        shot_text,
        primary="ollama",
        fallback="groq",
        fallback2="gemini",
        step_name="asset_keyword",
    )
    keyword = raw.strip().splitlines()[0].strip().strip("\"'.,")
    return keyword or "technology"


def extract_keywords_for_shots(shots: list[dict]) -> list[dict]:
    """
    Given an already-segmented shot list (each with a "text" field),
    returns one {shot_index, keyword, asset_type} per shot, index-aligned
    with the input list by construction. asset_type is always "photo" —
    video search/download is slower and more failure-prone, and the Ken
    Burns zoom already gives photos motion, so this trades a small amount
    of visual variety for reliability.
    """
    print(f"[AssetHunter] Extracting keywords for {len(shots)} pre-segmented shot(s)...")
    queries = []
    for i, shot in enumerate(shots):
        keyword = extract_keyword_for_shot(shot.get("text", ""))
        print(f"  [Shot {i}] '{keyword}'")
        queries.append({"shot_index": i, "keyword": keyword, "asset_type": "photo"})
    return queries


def run_asset_pipeline_for_shots(shots: list[dict], slug: str) -> list[dict]:
    """Like run_asset_pipeline(), but for an already-segmented shot list —
    see extract_keywords_for_shots() for why this exists as a separate
    path rather than reusing extract_shot_queries().

    Superseded by run_beat_pipeline_for_shots() below for the webapp's
    reel mode — kept as-is (one static photo per whole shot) since it's
    simpler and still correct for callers that don't need beat-level
    variety."""
    assets_dir = Path("output") / slug / "assets"
    queries = extract_keywords_for_shots(shots)
    manifest = fetch_assets(queries, assets_dir)

    manifest_path = Path("output") / slug / "asset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[AssetHunter] ✅ Manifest saved to {manifest_path}")

    return manifest


# ─────────────────────────────────────────────────────────────
# BEAT EXTRACTION — multiple distinct visual beats per shot
#
# Built in response to real feedback on the first version: one static
# photo held for a whole sentence (often 5-8s) reads as repetitive on a
# fast-scrolling platform — "laptop, coding screen, another laptop" —
# because one generic keyword repeated near-identically across shots
# finds near-identical Pexels stock photos every time. Fetching MORE
# photos with the SAME generic keywords wouldn't fix this; the actual
# fix is (a) more, shorter beats per shot (b) keywords that are specific
# to what's actually being said, actively steered away from repeating
# earlier ones, and (c) mixing in native motion-graphic beat types
# (stat callouts, icon bursts) that don't come from stock photos at all.
# ─────────────────────────────────────────────────────────────

TARGET_BEAT_SECONDS = 1.75
MAX_BEATS_PER_SHOT = 4
MIN_SHOT_SECONDS_FOR_BEATS = 1.5

# Icon beats pick from this fixed set (not freeform) so the Remotion side
# can map every returned concept straight to a hand-authored SVG — no
# "concept not found" guessing at render time.
ICON_CONCEPTS = [
    "money", "chart-up", "chart-down", "rocket", "robot", "warning",
    "checkmark", "lightbulb", "code", "handshake", "clock", "star",
]

BEAT_SYSTEM_PROMPT = f"""
You are a visual director for a fast-cut Instagram Reel about developer/tech topics.

Given ONE line of narration and a target number of visual "beats" for it, produce
that many DISTINCT beat descriptors — each a different visual angle on the line, so
the viewer never sees the same shot twice within it.

Each beat has a "type":
- "photo" — a Pexels stock photo. Give a concrete, SPECIFIC 2-4 word search query
  (e.g. "rocket launch pad", "stock market chart", "handshake office"). Avoid
  generic dev tropes like "developer typing" or "laptop screen" unless nothing
  more specific fits the line's actual content — generic queries return
  near-identical photos every time, which is the exact problem this is solving.
- "stat" — an animated on-screen text/number callout. Give VERY short "text"
  (1-5 words or a number, e.g. "$60B", "4x MORE EXPENSIVE", "60 SECONDS") pulled
  directly from the line's own content.
- "icons" — an icon-burst animation. Give one "concept" from EXACTLY this list:
  {", ".join(ICON_CONCEPTS)}

Rules:
- If given more than 1 beat, do NOT make them all "photo" — mix in at least one
  "stat" or "icons" beat.
- Never repeat a photo keyword or icon concept already used earlier in this reel
  (a list of already-used ones will be given to you when relevant — avoid close
  variations of those too, not just exact repeats).
- Never use a framework/product/company name (React, Python, Cursor, SpaceX, etc.)
  as a photo keyword — Pexels has no relevant results for those; describe the
  VISUAL the name evokes instead.

Respond ONLY with a valid JSON array, exactly the requested number of items, no
markdown, no preamble. Example for 3 beats:
[
  {{"type": "photo", "keyword": "rocket launch pad"}},
  {{"type": "stat", "text": "$60B"}},
  {{"type": "icons", "concept": "handshake"}}
]
""".strip()


def make_beat_agent(model):
    return create_agent(model=model, tools=[], system_prompt=BEAT_SYSTEM_PROMPT)


def extract_beats_for_shot(shot_text: str, num_beats: int, avoid: list[str]) -> list[dict]:
    """One LLM call for ALL beats of ONE shot (not one call per beat) —
    keeps API usage the same order of magnitude as the earlier
    one-keyword-per-shot design while still getting multiple distinct
    visuals per shot. Consumed immediately into this shot's own beat
    list, so it doesn't have the index-alignment risk extract_shot_queries()
    has (see that function's comment) — there's no separately-derived
    shot list it needs to line up against."""
    avoid_hint = (
        f"\n\nAlready used earlier in this reel, avoid repeating or closely varying: {', '.join(avoid[-10:])}"
        if avoid else ""
    )
    prompt = f'Line: "{shot_text}"\nNumber of beats needed: {num_beats}{avoid_hint}'

    raw = invoke_with_fallback(
        make_beat_agent,
        prompt,
        primary="ollama",
        fallback="groq",
        fallback2="gemini",
        step_name="asset_beats",
    )
    clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)

    try:
        beats = json.loads(clean)
        if not isinstance(beats, list) or not beats:
            raise ValueError("empty or non-list response")
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[AssetHunter] ⚠️  Beat JSON parse failed ({e}) — falling back to one generic photo beat.")
        return [{"type": "photo", "keyword": "technology"}]

    # Validate/normalize — never let one malformed entry crash the
    # pipeline; downgrade anything unparseable to a safe generic photo beat.
    cleaned = []
    for b in beats[:num_beats]:
        btype = b.get("type") if isinstance(b, dict) else None
        if btype == "photo" and b.get("keyword"):
            cleaned.append({"type": "photo", "keyword": str(b["keyword"]).strip()})
        elif btype == "stat" and b.get("text"):
            cleaned.append({"type": "stat", "text": str(b["text"]).strip()})
        elif btype == "icons":
            concept = str(b.get("concept", "")).strip().lower()
            if concept not in ICON_CONCEPTS:
                concept = "lightbulb"
            cleaned.append({"type": "icons", "concept": concept})
        else:
            cleaned.append({"type": "photo", "keyword": "technology"})

    # Safety net: if the model ignored the "mix it up" rule and every
    # beat came back as a photo despite asking for 2+, force the middle
    # one to an icon beat instead of depending entirely on prompt
    # compliance from whichever provider answered (weaker local models
    # especially don't always follow secondary instructions).
    if num_beats >= 2 and all(b["type"] == "photo" for b in cleaned):
        cleaned[len(cleaned) // 2] = {"type": "icons", "concept": "lightbulb"}

    while len(cleaned) < num_beats:
        cleaned.append({"type": "photo", "keyword": "technology"})

    return cleaned


def _fetch_photo_beat(keyword: str, shot_index: int, beat_index: int, assets_dir: Path) -> dict:
    """Fetch one Pexels photo for a single beat. Mirrors fetch_assets()'s
    per-item logic but keyed by (shot_index, beat_index) — a shot can now
    have multiple photo beats, so shot_index alone isn't a unique filename
    key anymore."""
    try:
        result = _search_pexels_photo(keyword)
        if result is None:
            print(f"    ⚠️  No result for '{keyword}' — using fallback placeholder.")
            return {"local_path": None, "credit": None, "pexels_url": None}

        download_url = result.get("src", {}).get("original") or result.get("src", {}).get("large")
        credit = result.get("photographer", "Pexels")
        pexels_url = result.get("url", "")
        if not download_url:
            return {"local_path": None, "credit": credit, "pexels_url": pexels_url}

        safe_keyword = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
        dest = assets_dir / f"shot-{shot_index:02d}-beat-{beat_index:02d}-{safe_keyword}.jpg"
        _download_file(download_url, dest)
        time.sleep(0.3)  # polite pacing between Pexels requests
        return {"local_path": str(dest), "credit": credit, "pexels_url": pexels_url}
    except requests.RequestException as e:
        print(f"    ❌ Fetch failed for '{keyword}': {e}")
        return {"local_path": None, "credit": None, "pexels_url": None}


def run_beat_pipeline_for_shots(shots: list[dict], slug: str) -> list[dict]:
    """
    Beat-level counterpart to run_asset_pipeline_for_shots() — splits each
    shot into ~TARGET_BEAT_SECONDS visual beats (from the shot's own
    (end - start) weight, capped at MAX_BEATS_PER_SHOT) instead of one
    static asset for the whole shot.

    Returns a flat manifest:
      [{"shot_index", "beat_index", "type", "keyword"|"text"|"concept",
        "local_path"?, "credit"?, "pexels_url"?}, ...]
    "photo" beats get fetched from Pexels; "stat"/"icons" beats need no
    download — video_composer.py passes their text/concept straight
    through to InstagramReel.tsx.
    """
    assets_dir = Path("output") / slug / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    used: list[str] = []
    beat_manifest: list[dict] = []

    print(f"[AssetHunter] Extracting beats for {len(shots)} shot(s)...")
    for shot_index, shot in enumerate(shots):
        duration = max(shot.get("end", 0) - shot.get("start", 0), MIN_SHOT_SECONDS_FOR_BEATS)
        num_beats = max(1, min(MAX_BEATS_PER_SHOT, round(duration / TARGET_BEAT_SECONDS)))

        beats = extract_beats_for_shot(shot.get("text", ""), num_beats, used)
        for beat_index, beat in enumerate(beats):
            entry = {"shot_index": shot_index, "beat_index": beat_index, **beat}
            if beat["type"] == "photo":
                print(f"  [Shot {shot_index}/Beat {beat_index}] photo: '{beat['keyword']}'")
                entry.update(_fetch_photo_beat(beat["keyword"], shot_index, beat_index, assets_dir))
                used.append(beat["keyword"])
            elif beat["type"] == "icons":
                print(f"  [Shot {shot_index}/Beat {beat_index}] icons: '{beat['concept']}'")
                used.append(beat["concept"])
            else:
                print(f"  [Shot {shot_index}/Beat {beat_index}] stat: '{beat['text']}'")
            beat_manifest.append(entry)

    manifest_path = Path("output") / slug / "asset_manifest.json"
    manifest_path.write_text(json.dumps(beat_manifest, indent=2))
    print(f"[AssetHunter] ✅ Beat manifest saved to {manifest_path} ({len(beat_manifest)} beats)")

    return beat_manifest


# ─────────────────────────────────────────────────────────────
# PEXELS FETCH + DOWNLOAD
# ─────────────────────────────────────────────────────────────

def _pexels_headers() -> dict:
    if not PEXELS_API_KEY or PEXELS_API_KEY == "your-pexels-api-key-here":
        raise EnvironmentError(
            "PEXELS_API_KEY is not set. "
            "Get a free key at https://www.pexels.com/api/ and add it to .env."
        )
    return {"Authorization": PEXELS_API_KEY}


def _search_pexels_photo(keyword: str) -> dict | None:
    """Search Pexels for a photo. Returns the best result dict or None."""
    resp = requests.get(
        PEXELS_PHOTO_URL,
        headers=_pexels_headers(),
        params={"query": keyword, "per_page": 5, "orientation": "portrait"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("photos", [])
    return results[0] if results else None


def _search_pexels_video(keyword: str) -> dict | None:
    """Search Pexels for a short video. Returns the best result dict or None."""
    resp = requests.get(
        PEXELS_VIDEO_URL,
        headers=_pexels_headers(),
        params={"query": keyword, "per_page": 5, "orientation": "portrait", "size": "medium"},
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("videos", [])
    return results[0] if results else None


def _pick_best_video_file(video_result: dict) -> str | None:
    """
    Pick the smallest video file that's at least 720p — we want the video
    fast to download but still high enough quality for a 1080p render.
    Pexels returns multiple resolution variants per video.
    """
    files = video_result.get("video_files", [])
    # Filter for HD-ish quality (height >= 720)
    hd_files = [f for f in files if f.get("height", 0) >= 720]
    if not hd_files:
        hd_files = files  # fall back to whatever is available
    # Sort by file_size ascending so we download the smallest HD variant
    hd_files.sort(key=lambda f: f.get("file_size", 9_999_999_999))
    return hd_files[0].get("link") if hd_files else None


def _download_file(url: str, dest: Path) -> None:
    """Stream-download a file to dest, with progress dots for large files."""
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    with open(dest, "wb") as fh:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            fh.write(chunk)


def fetch_assets(queries: list[dict], assets_dir: Path) -> list[dict]:
    """
    For each {shot_index, keyword, asset_type} query, search Pexels and
    download the best result. Returns an asset manifest list.

    Falls back from "video" to "photo" if no video match is found, and
    records a solid-color placeholder if Pexels has nothing at all so
    video_composer.py doesn't have to handle missing assets.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    manifest = []

    for q in queries:
        idx = q.get("shot_index", len(manifest))
        keyword = q.get("keyword", "technology")
        asset_type = q.get("asset_type", "photo")

        print(f"  [Shot {idx}] Searching Pexels: '{keyword}' ({asset_type})...")

        try:
            result = None
            actual_type = asset_type

            if asset_type == "video":
                result = _search_pexels_video(keyword)
                if result is None:
                    print(f"    → No video found, falling back to photo.")
                    result = _search_pexels_photo(keyword)
                    actual_type = "photo"
            else:
                result = _search_pexels_photo(keyword)

            if result is None:
                # Absolute last resort — use a plain dark fallback
                print(f"    ⚠️  No result for '{keyword}' — using fallback placeholder.")
                manifest.append({
                    "shot_index": idx,
                    "keyword": keyword,
                    "asset_type": "placeholder",
                    "local_path": None,
                    "pexels_url": None,
                    "credit": None,
                })
                continue

            # Build a clean filename
            if actual_type == "video":
                download_url = _pick_best_video_file(result)
                ext = ".mp4"
                credit = result.get("user", {}).get("name", "Pexels")
                pexels_url = result.get("url", "")
            else:
                download_url = result.get("src", {}).get("original") or result.get("src", {}).get("large")
                ext = ".jpg"
                credit = result.get("photographer", "Pexels")
                pexels_url = result.get("url", "")

            if not download_url:
                print(f"    ⚠️  No downloadable URL found for '{keyword}'.")
                manifest.append({
                    "shot_index": idx,
                    "keyword": keyword,
                    "asset_type": "placeholder",
                    "local_path": None,
                    "pexels_url": pexels_url,
                    "credit": credit,
                })
                continue

            # Sanitize keyword for filename
            safe_keyword = re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")
            dest = assets_dir / f"shot-{idx:02d}-{safe_keyword}{ext}"

            print(f"    → Downloading {actual_type} by {credit}...")
            _download_file(download_url, dest)

            manifest.append({
                "shot_index": idx,
                "keyword": keyword,
                "asset_type": actual_type,
                "local_path": str(dest),
                "pexels_url": pexels_url,
                "credit": credit,
            })

            # Be polite to the Pexels API — small delay between requests
            time.sleep(0.3)

        except requests.RequestException as e:
            print(f"    ❌ Fetch failed for '{keyword}': {e}")
            manifest.append({
                "shot_index": idx,
                "keyword": keyword,
                "asset_type": "placeholder",
                "local_path": None,
                "pexels_url": None,
                "credit": None,
            })

    return manifest


# ─────────────────────────────────────────────────────────────
# ORCHESTRATION
# ─────────────────────────────────────────────────────────────

def run_asset_pipeline(script: str, slug: str) -> list[dict]:
    """
    Full asset pipeline: script → keyword extraction → Pexels fetch.

    Args:
        script: The final (fact-checked) script text from run_pipeline().
        slug:   The slugified topic name used as the output folder key.

    Returns:
        Asset manifest list — [{shot_index, keyword, asset_type,
                                 local_path, pexels_url, credit}, ...]
    """
    assets_dir = Path("output") / slug / "assets"

    queries = extract_shot_queries(script)
    if not queries:
        print("[AssetHunter] No queries extracted — skipping Pexels fetch.")
        return []

    print(f"[AssetHunter] Fetching {len(queries)} assets from Pexels...")
    manifest = fetch_assets(queries, assets_dir)

    # Save manifest to disk so it can be inspected / reused without re-fetching
    manifest_path = Path("output") / slug / "asset_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[AssetHunter] ✅ Manifest saved to {manifest_path}")

    return manifest


# ─────────────────────────────────────────────────────────────
# STANDALONE ENTRY POINT (for testing)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Quick smoke-test: pass a topic as CLI arg, run the full pipeline
    # (needs PEXELS_API_KEY set in .env and the main script pipeline to have
    # already run so there's a script to read, OR pass --test for a canned script)
    if len(sys.argv) < 2:
        print("Usage: python asset_hunter.py <slug>")
        print("  Reads output/<slug>/script.txt and fetches assets.")
        sys.exit(1)

    _slug = sys.argv[1]
    _script_path = Path(f"output/{_slug}/script.txt")
    if not _script_path.exists():
        print(f"❌ No script found at {_script_path}")
        sys.exit(1)

    _script = _script_path.read_text()
    _manifest = run_asset_pipeline(_script, _slug)
    print(f"\n=== ASSET MANIFEST ({len(_manifest)} items) ===")
    for item in _manifest:
        print(f"  Shot {item['shot_index']}: [{item['asset_type']}] {item['keyword']} → {item.get('local_path', 'NO FILE')}")
