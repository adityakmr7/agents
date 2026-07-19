# webapp.py
#
# Local-only "paste a script, get a voiceover" page — same
# generate_voiceover() as everything else, reached over plain HTTP from
# your own browser instead of through Claude Desktop's MCP stdio
# transport. Sidesteps the whole problem class that caused MCP client
# tool-call timeouts and multiple independent Claude Desktop conversations
# each spinning up their own Chatterbox model instance and fighting over
# memory (see CLAUDE.md) — this is a single process, started once, used
# from one browser tab. A browser fetch() has no built-in timeout the way
# MCP's tool-call RPC does, so there's nothing to time out here even
# though generation still takes real time.

import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

from chatterbox.tts import ChatterboxTTS
from voiceover import generate_voiceover as _generate_voiceover

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT_ROOT / "output" / "webapp-voiceovers"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# Same model-caching pattern as mcp_server.py — loading is the slow part,
# and this process stays alive across requests, so load once.
_tts_model: ChatterboxTTS | None = None
_tts_model_lock = threading.Lock()


def _get_tts_model(device: str = "mps") -> ChatterboxTTS:
    global _tts_model
    if _tts_model is None:
        with _tts_model_lock:
            if _tts_model is None:
                print("Loading Chatterbox model (first request this session)...")
                _tts_model = ChatterboxTTS.from_pretrained(device=device)
    return _tts_model


_jobs: dict[str, dict] = {}


def _run_job(job_id: str, script_text: str, reference_path: str) -> None:
    try:
        output_path = OUTPUT_DIR / f"voiceover-{job_id}.wav"
        model = _get_tts_model()
        _generate_voiceover(
            script_text,
            reference_path=reference_path,
            output_path=str(output_path),
            model=model,
        )
        _jobs[job_id] = {"status": "done", "filename": output_path.name}
    except Exception as e:
        _jobs[job_id] = {"status": "error", "error": f"{type(e).__name__}: {e}"}


PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Voiceover generator</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 720px; margin: 60px auto; padding: 0 20px; color: #1a1a1a; }
  h1 { font-size: 20px; }
  textarea { width: 100%; height: 260px; font-size: 15px; padding: 12px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 8px; font-family: inherit; }
  input[type=text] { width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #ccc; border-radius: 6px; font-family: inherit; }
  label { display: block; margin: 16px 0 6px; font-size: 13px; color: #555; }
  button { margin-top: 16px; padding: 10px 20px; font-size: 15px; border: none; border-radius: 6px; background: #1a1a1a; color: white; cursor: pointer; }
  button:disabled { background: #999; cursor: default; }
  #status { margin-top: 16px; font-size: 14px; color: #555; }
  #result { margin-top: 20px; }
  audio { width: 100%; margin-top: 10px; }
  a.download { display: inline-block; margin-top: 10px; }
</style>
</head>
<body>
  <h1>Paste a script &rarr; get a voiceover</h1>
  <textarea id="script" placeholder="Paste your final script here..."></textarea>
  <label for="voice">Reference voice file (in dev-research-agent/, default aditya-voice.m4a)</label>
  <input type="text" id="voice" value="aditya-voice.m4a">
  <br>
  <button id="go">Generate voiceover</button>
  <div id="status"></div>
  <div id="result"></div>

<script>
const goBtn = document.getElementById('go');
const statusEl = document.getElementById('status');
const resultEl = document.getElementById('result');
let startTime;

goBtn.addEventListener('click', async () => {
  const script_text = document.getElementById('script').value.trim();
  const reference_path = document.getElementById('voice').value.trim() || 'aditya-voice.m4a';
  if (!script_text) { statusEl.textContent = 'Paste a script first.'; return; }

  goBtn.disabled = true;
  resultEl.innerHTML = '';
  startTime = Date.now();

  const startRes = await fetch('/generate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({script_text, reference_path})
  });
  const startData = await startRes.json();
  if (!startRes.ok) {
    statusEl.textContent = 'Error: ' + (startData.error || 'could not start');
    goBtn.disabled = false;
    return;
  }
  const job_id = startData.job_id;

  const poll = async () => {
    const r = await fetch('/status/' + job_id);
    const data = await r.json();
    const elapsed = Math.round((Date.now() - startTime) / 1000);

    if (data.status === 'running') {
      statusEl.textContent = `Generating... (${elapsed}s — first run this session loads the model, which is the slow part)`;
      setTimeout(poll, 2000);
    } else if (data.status === 'done') {
      statusEl.textContent = `Done in ${elapsed}s.`;
      resultEl.innerHTML = `
        <audio controls src="/audio/${data.filename}"></audio><br>
        <a class="download" href="/audio/${data.filename}" download>Download ${data.filename}</a>
      `;
      goBtn.disabled = false;
    } else if (data.status === 'error') {
      statusEl.textContent = 'Error: ' + data.error;
      goBtn.disabled = false;
    }
  };
  poll();
});
</script>
</body>
</html>
"""


@app.route("/")
def index() -> str:
    return PAGE


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

    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"status": "running"}
    thread = threading.Thread(
        target=_run_job, args=(job_id, script_text, str(ref)), daemon=True
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


if __name__ == "__main__":
    port = 5959
    url = f"http://127.0.0.1:{port}"
    print(f"Voiceover app running at {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
