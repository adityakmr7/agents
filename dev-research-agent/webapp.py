# webapp.py
#
# Local-only Flask UI with two modes:
#
#   1. Voiceover-only  — paste a script, get a .wav back.  (original)
#   2. Full pipeline   — paste YOUR OWN script + a topic name, get a
#                        voiceover AND a rendered video without running
#                        the AI research/draft/critique pipeline at all.
#
# Mode 2 reuses create_video_assets_from_script() defined here, which is
# identical to main.create_video_assets() except it takes a ready-made
# script string instead of calling run_pipeline(). No changes to agent.py
# or main.py are required.

import threading
import uuid
import webbrowser
import re
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from chatterbox.tts import ChatterboxTTS
from voiceover import generate_voiceover as _generate_voiceover
from render import render_from_script_and_audio

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "webapp-voiceovers"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# MODEL CACHE — loaded once, reused across all requests
# ─────────────────────────────────────────────────────────────

_tts_model: ChatterboxTTS | None = None
_tts_model_lock = threading.Lock()

# Separate from _tts_model_lock, which only guards the one-time lazy load.
# generate() itself must also be serialized — confirmed with a real stuck
# run: two jobs submitted close together both called model.generate() on
# the SAME shared instance concurrently, and the process sat at 150-200%
# CPU for 25+ minutes without producing output. Chatterbox/PyTorch on MPS
# isn't safe for concurrent multi-threaded inference on one model
# instance; every generation call must go through this lock, one job at a
# time, even after the model is already loaded.
_generation_lock = threading.Lock()


def _get_tts_model(device: str = "mps") -> ChatterboxTTS:
    global _tts_model
    if _tts_model is None:
        with _tts_model_lock:
            if _tts_model is None:
                print("Loading Chatterbox model (first request this session)...")
                _tts_model = ChatterboxTTS.from_pretrained(device=device)
    return _tts_model


def _generate_voiceover_serialized(
    script_text: str,
    reference_path: str,
    output_path: str,
    exaggeration: float = 0.7,
    cfg_weight: float = 0.3,
    temperature: float = 0.75,
) -> str:
    """Wraps voiceover.generate_voiceover() with _generation_lock — the
    single call site every job runner below should use instead of calling
    _generate_voiceover directly. exaggeration/cfg_weight/temperature
    default to the same values voiceover.py itself defaults to."""
    model = _get_tts_model()
    with _generation_lock:
        return _generate_voiceover(
            script_text,
            reference_path=reference_path,
            output_path=output_path,
            model=model,
            exaggeration=exaggeration,
            cfg_weight=cfg_weight,
            temperature=temperature,
        )


def _voice_params_from_request(data: dict) -> dict:
    """Parses optional exaggeration/cfg_weight/temperature overrides from
    a request body — shared by all three /generate* routes. Missing or
    invalid values fall back to voiceover.py's own defaults (silently,
    not an error — these are creative tuning knobs, not required input)."""
    defaults = {"exaggeration": 0.7, "cfg_weight": 0.3, "temperature": 0.75}
    out = {}
    for key, default in defaults.items():
        try:
            out[key] = float(data.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


# ─────────────────────────────────────────────────────────────
# JOB STORE — keyed by uuid hex
# ─────────────────────────────────────────────────────────────

_jobs: dict[str, dict] = {}


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:50]


# ─────────────────────────────────────────────────────────────
# BACKGROUND JOB RUNNERS
# ─────────────────────────────────────────────────────────────

def _run_voiceover_job(
    job_id: str, script_text: str, reference_path: str, voice_params: dict
) -> None:
    """Mode 1: voiceover only — original behaviour."""
    try:
        output_path = OUTPUT_DIR / f"voiceover-{job_id}.wav"
        _generate_voiceover_serialized(script_text, reference_path, str(output_path), **voice_params)
        _jobs[job_id] = {"status": "done", "filename": output_path.name}
    except Exception as e:
        _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _run_full_pipeline_job(
    job_id: str, script_text: str, topic: str, reference_path: str, voice_params: dict
) -> None:
    """Mode 2: BYO script — voiceover + Remotion video render, no AI generation."""
    try:
        slug = _slugify(topic) or job_id
        out_dir = PROJECT_ROOT / "output" / slug
        out_dir.mkdir(parents=True, exist_ok=True)

        # Save the pasted script so render_from_script_and_audio has a path
        script_path = out_dir / "script.txt"
        script_path.write_text(script_text)

        # --- Step 1: voiceover ---
        _jobs[job_id]["step"] = "Generating voiceover..."
        audio_path = out_dir / "voiceover.wav"
        _generate_voiceover_serialized(script_text, reference_path, str(audio_path), **voice_params)

        # --- Step 2: Remotion render ---
        _jobs[job_id]["step"] = "Rendering video..."
        video_path = render_from_script_and_audio(
            script_text=script_text,
            audio_path=str(audio_path),
            slug=slug,
        )

        _jobs[job_id] = {
            "status": "done",
            "audio_filename": audio_path.name,
            "audio_dir": str(out_dir),   # so the /audio-full/<job_id> route can find it
            "video_path": video_path,
            "slug": slug,
        }
    except Exception as e:
        _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


def _run_reel_job(
    job_id: str, script_text: str, topic: str, reference_path: str, voice_params: dict
) -> None:
    """Mode 3: BYO script -> voiceover -> Pexels/motion-graphic beat hunt
    -> Instagram Reel render. Calls video_composer functions directly
    rather than going through the mcp_server.py hunt_assets/
    render_instagram_reel tools — those exist for the Claude-Desktop-driven
    path and (as of this writing) hunt_assets has a real bug where it never
    stages downloaded assets into Remotion's public/ folder despite
    render_instagram_reel's docstring claiming it does. video_composer's
    functions do the staging correctly internally, so this mode is
    unaffected by that bug rather than depending on a fix to it.

    Uses the BEAT pipeline (asset_hunter.run_beat_pipeline_for_shots +
    video_composer.compose_instagram_reel_from_beats), not the older
    one-static-photo-per-shot path — built after real feedback that one
    photo held for a whole 5-8s sentence reads as repetitive ("laptop,
    coding screen, another laptop") on a fast-scrolling platform. Each
    shot now gets multiple ~1.5-2s visual beats mixing Pexels photos with
    native stat-callout/icon-burst motion graphics; see asset_hunter.py's
    beat-extraction comment for the full reasoning.
    """
    try:
        from video_composer import _sentences_to_shots, compose_instagram_reel_from_beats
        from asset_hunter import run_beat_pipeline_for_shots

        slug = _slugify(topic) or job_id
        out_dir = PROJECT_ROOT / "output" / slug
        out_dir.mkdir(parents=True, exist_ok=True)

        script_path = out_dir / "script.txt"
        script_path.write_text(script_text)

        # --- Step 1: voiceover ---
        _jobs[job_id]["step"] = "Generating voiceover..."
        audio_path = out_dir / "voiceover.wav"
        _generate_voiceover_serialized(script_text, reference_path, str(audio_path), **voice_params)

        # --- Step 2: segment into shots (deterministic, Python-side) ---
        shots = _sentences_to_shots(script_text)
        if not shots:
            raise ValueError("Script produced no shots after cleaning — is it empty?")

        # --- Step 3: hunt beats — multiple distinct visuals per shot ---
        _jobs[job_id]["step"] = f"Finding visuals for {len(shots)} shot(s)..."
        beat_manifest = run_beat_pipeline_for_shots(shots, slug)

        # --- Step 4: stage assets + render InstagramReel ---
        _jobs[job_id]["step"] = "Rendering reel..."
        reel_path = compose_instagram_reel_from_beats(
            shots=shots,
            beat_manifest=beat_manifest,
            audio_path=str(audio_path),
            topic=topic,
            slug=slug,
        )

        photo_beats = [b for b in beat_manifest if b["type"] == "photo"]
        asset_count = sum(1 for b in photo_beats if b.get("local_path"))
        _jobs[job_id] = {
            "status": "done",
            "audio_filename": audio_path.name,
            "audio_dir": str(out_dir),
            "video_path": reel_path,
            "slug": slug,
            "asset_count": asset_count,
            "shot_count": len(shots),
            "beat_count": len(beat_manifest),
        }
    except Exception as e:
        _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


# ─────────────────────────────────────────────────────────────
# HTML PAGE
# ─────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Dev Video Generator</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    background: #0f0f13;
    color: #e8e8ec;
    min-height: 100vh;
    padding: 40px 20px 80px;
  }

  .container { max-width: 740px; margin: 0 auto; }

  h1 {
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.3px;
    margin-bottom: 6px;
  }
  .subtitle { font-size: 13px; color: #888; margin-bottom: 28px; }

  /* ── mode toggle ── */
  .mode-toggle {
    display: flex;
    gap: 0;
    border: 1px solid #2a2a35;
    border-radius: 8px;
    overflow: hidden;
    width: fit-content;
    margin-bottom: 24px;
  }
  .mode-btn {
    padding: 8px 18px;
    font-size: 13px;
    font-weight: 500;
    background: transparent;
    color: #888;
    border: none;
    cursor: pointer;
    transition: background 0.15s, color 0.15s;
  }
  .mode-btn.active {
    background: #1e1e2e;
    color: #c9b1ff;
  }
  .mode-btn:not(:last-child) { border-right: 1px solid #2a2a35; }

  /* ── form fields ── */
  .field { margin-bottom: 16px; }

  .advanced {
    margin-bottom: 16px;
    border: 1px solid #2a2a35;
    border-radius: 8px;
    padding: 2px 14px;
  }
  .advanced summary {
    cursor: pointer;
    padding: 10px 0;
    font-size: 13px;
    color: #a29bfe;
    font-weight: 500;
  }
  .advanced .field:last-of-type { margin-bottom: 14px; }
  .advanced label { text-transform: none; letter-spacing: normal; font-size: 12px; }
  label {
    display: block;
    font-size: 12px;
    font-weight: 500;
    color: #888;
    margin-bottom: 6px;
    text-transform: uppercase;
    letter-spacing: 0.5px;
  }
  textarea, input[type=text] {
    width: 100%;
    padding: 12px 14px;
    font-size: 14px;
    font-family: inherit;
    background: #1a1a24;
    border: 1px solid #2a2a35;
    border-radius: 8px;
    color: #e8e8ec;
    outline: none;
    transition: border-color 0.15s;
  }
  textarea:focus, input[type=text]:focus { border-color: #6c5ce7; }
  textarea { height: 220px; resize: vertical; line-height: 1.55; }

  /* ── badge ── */
  .badge {
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 20px;
    margin-left: 8px;
    vertical-align: middle;
    background: #2d2040;
    color: #c9b1ff;
  }

  /* ── button ── */
  #go {
    margin-top: 4px;
    padding: 11px 26px;
    font-size: 14px;
    font-weight: 600;
    border: none;
    border-radius: 8px;
    background: linear-gradient(135deg, #6c5ce7, #a29bfe);
    color: #fff;
    cursor: pointer;
    transition: opacity 0.15s;
  }
  #go:disabled { opacity: 0.45; cursor: default; }

  /* ── status / result ── */
  #status {
    margin-top: 18px;
    font-size: 13px;
    color: #888;
    min-height: 20px;
  }
  .step-tag {
    display: inline-block;
    background: #1e1e2e;
    border: 1px solid #2a2a35;
    border-radius: 4px;
    padding: 2px 8px;
    font-size: 12px;
    color: #a29bfe;
    margin-bottom: 4px;
  }
  #result { margin-top: 20px; }
  audio { width: 100%; margin-top: 10px; border-radius: 6px; }
  .result-row { margin-top: 12px; display: flex; gap: 10px; flex-wrap: wrap; }
  .result-row a {
    font-size: 13px;
    padding: 7px 14px;
    border-radius: 6px;
    background: #1e1e2e;
    border: 1px solid #2a2a35;
    color: #a29bfe;
    text-decoration: none;
    transition: border-color 0.15s;
  }
  .result-row a:hover { border-color: #6c5ce7; }
  .video-path {
    margin-top: 10px;
    font-size: 12px;
    font-family: "SF Mono", "Fira Code", monospace;
    color: #555;
    word-break: break-all;
  }

  .hidden { display: none !important; }
</style>
</head>
<body>
<div class="container">
  <h1>Dev Video Generator</h1>
  <p class="subtitle">Paste a script and get a voiceover — or go all the way to a rendered video.</p>

  <!-- Mode toggle -->
  <div class="mode-toggle">
    <button class="mode-btn active" id="modeVoiceover" onclick="setMode('voiceover')">
      🎙 Voiceover only
    </button>
    <button class="mode-btn" id="modeFull" onclick="setMode('full')">
      🎬 Full video <span class="badge">BYO script</span>
    </button>
    <button class="mode-btn" id="modeReel" onclick="setMode('reel')">
      ✨ Instagram Reel <span class="badge">+ visuals</span>
    </button>
  </div>

  <!-- Topic (Full + Reel modes) -->
  <div class="field hidden" id="topicField">
    <label for="topic">Video topic / title</label>
    <input type="text" id="topic" placeholder="e.g. React's useTransition hook">
  </div>

  <!-- Script -->
  <div class="field">
    <label for="script">Your script</label>
    <textarea id="script" placeholder="Paste your final script here..."></textarea>
  </div>

  <!-- Voice reference -->
  <div class="field">
    <label for="voice">Reference voice file (filename inside dev-research-agent/)</label>
    <input type="text" id="voice" value="aditya-voice.m4a">
  </div>

  <!-- Advanced voice tuning -->
  <details class="advanced">
    <summary>Advanced voice settings</summary>
    <div class="field">
      <label for="exaggeration">Exaggeration (0-1, default 0.7) — emotional/vocal dynamics. Higher = more animated, too high = overdone.</label>
      <input type="text" id="exaggeration" value="0.7">
    </div>
    <div class="field">
      <label for="cfgWeight">CFG weight (0-1, default 0.3) — lower = faster pacing, less robotic cadence.</label>
      <input type="text" id="cfgWeight" value="0.3">
    </div>
    <div class="field">
      <label for="temperature">Temperature (0-1+, default 0.75) — higher = more variation between chunks.</label>
      <input type="text" id="temperature" value="0.75">
    </div>
  </details>

  <button id="go">Generate</button>

  <div id="status"></div>
  <div id="result"></div>
</div>

<script>
let currentMode = 'voiceover';
let startTime;

const MODE_BUTTON_TEXT = {
  voiceover: 'Generate voiceover',
  full: 'Generate voiceover + video',
  reel: 'Generate Instagram Reel',
};

function setMode(mode) {
  currentMode = mode;
  document.getElementById('modeVoiceover').classList.toggle('active', mode === 'voiceover');
  document.getElementById('modeFull').classList.toggle('active', mode === 'full');
  document.getElementById('modeReel').classList.toggle('active', mode === 'reel');
  document.getElementById('topicField').classList.toggle('hidden', mode === 'voiceover');
  document.getElementById('go').textContent = MODE_BUTTON_TEXT[mode];
  document.getElementById('result').innerHTML = '';
  document.getElementById('status').textContent = '';
}

const goBtn = document.getElementById('go');
const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');

function getVoiceParams() {
  return {
    exaggeration: document.getElementById('exaggeration').value.trim(),
    cfg_weight: document.getElementById('cfgWeight').value.trim(),
    temperature: document.getElementById('temperature').value.trim(),
  };
}

goBtn.addEventListener('click', async () => {
  const script_text = document.getElementById('script').value.trim();
  const reference_path = document.getElementById('voice').value.trim() || 'aditya-voice.m4a';

  if (!script_text) { statusEl.textContent = 'Paste a script first.'; return; }

  if (currentMode === 'full' || currentMode === 'reel') {
    const topic = document.getElementById('topic').value.trim();
    if (!topic) { statusEl.textContent = 'Enter a topic / title for the video.'; return; }
    if (currentMode === 'full') {
      await runFull(script_text, topic, reference_path);
    } else {
      await runReel(script_text, topic, reference_path);
    }
  } else {
    await runVoiceoverOnly(script_text, reference_path);
  }
});

// ── Mode 1: voiceover only ──────────────────────────────────
async function runVoiceoverOnly(script_text, reference_path) {
  goBtn.disabled = true;
  resultEl.innerHTML = '';
  startTime = Date.now();

  const startRes = await fetch('/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({script_text, reference_path, ...getVoiceParams()})
  });
  const startData = await startRes.json();
  if (!startRes.ok) {
    statusEl.textContent = 'Error: ' + (startData.error || 'could not start');
    goBtn.disabled = false;
    return;
  }
  pollVoiceover(startData.job_id);
}

async function pollVoiceover(job_id) {
  const r = await fetch('/status/' + job_id);
  const data = await r.json();
  const elapsed = Math.round((Date.now() - startTime) / 1000);

  if (data.status === 'running') {
    statusEl.textContent = `Generating voiceover... (${elapsed}s)`;
    setTimeout(() => pollVoiceover(job_id), 2000);
  } else if (data.status === 'done') {
    statusEl.textContent = `Done in ${elapsed}s.`;
    resultEl.innerHTML = `
      <audio controls src="/audio/${data.filename}"></audio>
      <div class="result-row">
        <a href="/audio/${data.filename}" download>⬇ Download .wav</a>
      </div>`;
    goBtn.disabled = false;
  } else {
    statusEl.textContent = 'Error: ' + data.error;
    goBtn.disabled = false;
  }
}

// ── Mode 2: full pipeline (BYO script) ─────────────────────
async function runFull(script_text, topic, reference_path) {
  goBtn.disabled = true;
  resultEl.innerHTML = '';
  startTime = Date.now();

  const startRes = await fetch('/generate-full', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({script_text, topic, reference_path, ...getVoiceParams()})
  });
  const startData = await startRes.json();
  if (!startRes.ok) {
    statusEl.textContent = 'Error: ' + (startData.error || 'could not start');
    goBtn.disabled = false;
    return;
  }
  pollFull(startData.job_id);
}

async function pollFull(job_id) {
  const r = await fetch('/status/' + job_id);
  const data = await r.json();
  const elapsed = Math.round((Date.now() - startTime) / 1000);

  if (data.status === 'running') {
    const step = data.step || 'Working...';
    statusEl.innerHTML = `<span class="step-tag">${step}</span> ${elapsed}s elapsed`;
    setTimeout(() => pollFull(job_id), 2000);
  } else if (data.status === 'done') {
    statusEl.textContent = `Done in ${elapsed}s.`;
    resultEl.innerHTML = `
      <video controls src="/video/${job_id}" style="width:100%;max-width:340px;border-radius:8px;display:block;margin-top:6px;"></video>
      <div class="result-row">
        <a href="/audio-full/${job_id}" download>⬇ Download .wav</a>
        <a href="/video/${job_id}" download>⬇ Download .mp4</a>
      </div>
      <div class="video-path">📹 Video saved to: ${data.video_path}</div>`;
    goBtn.disabled = false;
  } else {
    statusEl.textContent = 'Error: ' + data.error;
    goBtn.disabled = false;
  }
}

// ── Mode 3: Instagram Reel (BYO script + Pexels assets) ─────
async function runReel(script_text, topic, reference_path) {
  goBtn.disabled = true;
  resultEl.innerHTML = '';
  startTime = Date.now();

  const startRes = await fetch('/generate-reel', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({script_text, topic, reference_path, ...getVoiceParams()})
  });
  const startData = await startRes.json();
  if (!startRes.ok) {
    statusEl.textContent = 'Error: ' + (startData.error || 'could not start');
    goBtn.disabled = false;
    return;
  }
  pollReel(startData.job_id);
}

async function pollReel(job_id) {
  const r = await fetch('/status/' + job_id);
  const data = await r.json();
  const elapsed = Math.round((Date.now() - startTime) / 1000);

  if (data.status === 'running') {
    const step = data.step || 'Working...';
    statusEl.innerHTML = `<span class="step-tag">${step}</span> ${elapsed}s elapsed`;
    setTimeout(() => pollReel(job_id), 2000);
  } else if (data.status === 'done') {
    statusEl.textContent = `Done in ${elapsed}s — ${data.beat_count} visual beats across ${data.shot_count} shots (${data.asset_count} from Pexels, rest motion graphics).`;
    resultEl.innerHTML = `
      <video controls src="/video/${job_id}" style="width:100%;max-width:340px;border-radius:8px;display:block;margin-top:6px;"></video>
      <div class="result-row">
        <a href="/audio-full/${job_id}" download>⬇ Download .wav</a>
        <a href="/video/${job_id}" download>⬇ Download reel .mp4</a>
      </div>
      <div class="video-path">📹 Reel saved to: ${data.video_path}</div>`;
    goBtn.disabled = false;
  } else {
    statusEl.textContent = 'Error: ' + data.error;
    goBtn.disabled = false;
  }
}
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index() -> str:
    return PAGE


# ── Mode 1: voiceover only ───────────────────────────────────

@app.route("/generate", methods=["POST"])
def generate() -> tuple[Response, int] | Response:
    data = request.get_json(force=True)
    script_text = (data.get("script_text") or "").strip()
    reference_path = (data.get("reference_path") or "aditya-voice.m4a").strip()

    if not script_text:
        return jsonify({"error": "script_text is required"}), 400

    ref = Path(reference_path)
    if not ref.is_absolute():
        ref = PROJECT_ROOT / ref
    if not ref.exists():
        return jsonify({"error": f"Reference voice clip not found: {ref}"}), 400

    voice_params = _voice_params_from_request(data)
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"status": "running"}
    thread = threading.Thread(
        target=_run_voiceover_job, args=(job_id, script_text, str(ref), voice_params), daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id: str) -> tuple[Response, int] | Response:
    job = _jobs.get(job_id)
    if job is None:
        return jsonify({"status": "error", "error": "unknown job_id"}), 404
    return jsonify(job)


@app.route("/audio/<filename>")
def audio(filename: str):
    return send_from_directory(OUTPUT_DIR, filename)


# ── Mode 2: full pipeline (BYO script) ──────────────────────

@app.route("/generate-full", methods=["POST"])
def generate_full() -> tuple[Response, int] | Response:
    data = request.get_json(force=True)
    script_text = (data.get("script_text") or "").strip()
    topic = (data.get("topic") or "").strip()
    reference_path = (data.get("reference_path") or "aditya-voice.m4a").strip()

    if not script_text:
        return jsonify({"error": "script_text is required"}), 400
    if not topic:
        return jsonify({"error": "topic is required"}), 400

    ref = Path(reference_path)
    if not ref.is_absolute():
        ref = PROJECT_ROOT / ref
    if not ref.exists():
        return jsonify({"error": f"Reference voice clip not found: {ref}"}), 400

    voice_params = _voice_params_from_request(data)
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"status": "running", "step": "Starting..."}
    thread = threading.Thread(
        target=_run_full_pipeline_job,
        args=(job_id, script_text, topic, str(ref), voice_params),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/audio-full/<job_id>")
def audio_full(job_id: str):
    """Serve the voiceover for a full-pipeline OR reel job — same job dict
    shape (audio_dir/audio_filename), one route covers both."""
    job = _jobs.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "not ready or unknown job"}), 404
    audio_dir = Path(job["audio_dir"])
    return send_from_directory(audio_dir, job["audio_filename"])


@app.route("/video/<job_id>")
def video(job_id: str):
    """Serve the rendered .mp4 for in-browser preview — full-pipeline and
    reel jobs both set video_path, so one route covers both."""
    job = _jobs.get(job_id)
    if not job or job.get("status") != "done" or not job.get("video_path"):
        return jsonify({"error": "not ready or unknown job"}), 404
    video_path = Path(job["video_path"])
    return send_from_directory(video_path.parent, video_path.name)


# ── Mode 3: Instagram Reel (BYO script + Pexels assets) ─────

@app.route("/generate-reel", methods=["POST"])
def generate_reel() -> tuple[Response, int] | Response:
    data = request.get_json(force=True)
    script_text = (data.get("script_text") or "").strip()
    topic = (data.get("topic") or "").strip()
    reference_path = (data.get("reference_path") or "aditya-voice.m4a").strip()

    if not script_text:
        return jsonify({"error": "script_text is required"}), 400
    if not topic:
        return jsonify({"error": "topic is required"}), 400

    ref = Path(reference_path)
    if not ref.is_absolute():
        ref = PROJECT_ROOT / ref
    if not ref.exists():
        return jsonify({"error": f"Reference voice clip not found: {ref}"}), 400

    voice_params = _voice_params_from_request(data)
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"status": "running", "step": "Starting..."}
    thread = threading.Thread(
        target=_run_reel_job,
        args=(job_id, script_text, topic, str(ref), voice_params),
        daemon=True,
    )
    thread.start()
    return jsonify({"job_id": job_id})


# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = 5959
    url = f"http://127.0.0.1:{port}"
    print(f"Voiceover app running at {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
