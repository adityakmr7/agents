# Changelog

All changes to this project are documented here.
Format: `[YYYY-MM-DD] FILE — What changed and why.`

---

## 2026-07-19 — Instagram Video Generation Feature

### [NEW] `asset_hunter.py`
- Added `AssetHunterAgent` — an LLM agent (Gemini/Ollama with fallback) that reads a
  final script and extracts one visual keyword query per shot.
- Added `fetch_assets()` — downloads matching images from the Pexels API (free, CC0)
  to `output/<slug>/assets/`, returning a structured asset manifest.
- Added `run_asset_pipeline()` — orchestrates keyword extraction → Pexels fetch in one call.

### [NEW] `video_composer.py`
- Added `compose_instagram_video()` — takes shot list, asset manifest, and voiceover
  path; writes `reel-props.json` for the new Remotion `InstagramReel` composition
  and shells out to render it.
- Output format: 1080×1920 (9:16 vertical), H.264, ≤ 90s — Instagram Reels ready.

### [MODIFIED] `render.py`
- Added `render_reel()` — sister to `render_shots_to_video()`, but targets the new
  `InstagramReel` Remotion composition.
- Copies all per-shot asset files into Remotion's `public/assets/<slug>/` folder
  before rendering so Remotion can resolve them by relative URL.

### [NEW] `src/InstagramReel.tsx` (in `/motion` Remotion project)
- New Remotion composition at 1080×1920 (9:16).
- Per-shot structure: Ken Burns background (image with slow zoom/pan), animated
  caption text (word-by-word highlight), dark gradient overlay for legibility,
  and a thin progress bar tracking playback.
- Props: `audioFileName`, `shots` (each with `text`, `assetFile`, `assetType`),
  `topic`.

### [MODIFIED] `src/Root.tsx` (in `/motion` Remotion project)
- Registered `InstagramReel` composition (id: `"InstagramReel"`, 1080×1920, 30fps).

### [MODIFIED] `agent.py`
- Added `make_asset_analyst(model)` — LLM agent factory for extracting per-shot
  visual keywords from a finished script. Returns JSON list of
  `{shot_index, keyword, asset_type}`.

### [MODIFIED] `mcp_server.py`
- Added `hunt_assets(script_text, slug)` MCP tool — background job that runs
  `AssetHunterAgent` + Pexels fetch. Returns `job_id`; poll `check_job_status`.
- Added `render_instagram_reel(shots_json, asset_manifest_json, audio_path, topic)`
  MCP tool — background job that runs the full reel render. Returns `job_id`.

### [MODIFIED] `main.py`
- Added `--instagram` CLI flag — when set, runs the full asset hunt + reel render
  pipeline after script + voiceover steps.
- Added `run_instagram_pipeline()` helper that sequences the new agents.

### [MODIFIED] `.env`
- Added `PEXELS_API_KEY` placeholder — must be filled in before running
  `asset_hunter.py`. Free key at https://www.pexels.com/api/.
