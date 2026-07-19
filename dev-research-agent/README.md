# Dev Content Pipeline — Setup Guide

Turns a topic into a finished vertical (1080×1920) video: researched
script, cloned-voice narration, rendered motion-graphic captions. Two
sibling projects, three ways to run it — see [CLAUDE.md](CLAUDE.md) for
the full architecture and known gotchas; this doc is just setup.

```
parent-folder/
  dev-research-agent/   <- this project (Python)
  motion/                <- Remotion renderer (TypeScript/React)
```

## Three ways to use it

| | Batch pipeline | Interactive MCP tools | Local web app |
|---|---|---|---|
| **Entry point** | `videogen` CLI / `main.py` | Claude Desktop | `voiceover-app` / `webapp.py` |
| **Who drafts/critiques the script** | A second LLM (Gemini/Ollama), unattended | Claude, live in conversation | You (paste a finished script) |
| **Use when** | Scripted/unattended runs | You're already talking to Claude Desktop | You just want a voiceover fast, no MCP reliability concerns |
| **Produces** | Script + voiceover + video | Script + voiceover + video | Voiceover only |

All three share the same mechanical steps underneath (search, TTS,
render), so most of this setup applies to all of them. The web app is the
simplest and most reliable path if you just need a voiceover from
something you've already written — see "Local web app" below.

---

## Prerequisites (both platforms)

- **Python 3.11–3.14** (developed against 3.14 — see CLAUDE.md's "Python
  3.14 / dependency chain" gotchas if you hit `pkg_resources` or
  `torchcodec` errors regardless of OS)
- **Node.js 18+** (for `motion/`'s Remotion renderer)
- **ffmpeg**
- **API keys:**
  - `TAVILY_API_KEY` — required for both entry points (web search)
  - `GEMINI_API_KEY` — only required for the **batch pipeline** (the MCP
    tools do no LLM reasoning, so they never touch this)
  - `OLLAMA_MODEL` + a running [Ollama](https://ollama.com) — optional,
    only used as the batch pipeline's fallback if Gemini fails
- **A reference voice clip** for cloning (~5s, clean single-speaker audio).
  The default filename both entry points use is `aditya-voice.m4a`, placed
  in the project root — swap in your own clip under that name, or pass
  `reference_path` explicitly to use a different file/name.

---

## macOS setup

```bash
cd dev-research-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

brew install ffmpeg   # if torchcodec errors on `torchaudio.save()`

# Node for motion/ — via nvm (https://github.com/nvm-sh/nvm) or the
# official installer, either is fine
cd ../motion && npm install && cd ../dev-research-agent
```

Create `dev-research-agent/.env`:

```bash
TAVILY_API_KEY=...
GEMINI_API_KEY=...          # only needed for the batch pipeline
MODEL_PROVIDER=gemini
OLLAMA_MODEL=llama3.1:8b    # only needed if you want the Ollama fallback
```

Chatterbox TTS defaults to `device="mps"` in `voiceover.py` — correct
as-is on Apple Silicon. No edit needed.

**Test the batch pipeline:**

```bash
python3 main.py "A quick test topic" --no-render
```

**Optional — install the `videogen` CLI on PATH** (bash/zsh only):

```bash
chmod +x videogen
ln -sf "$(pwd)/videogen" ~/.local/bin/videogen   # or any dir already on your PATH
videogen "A quick test topic" --no-render
```

---

## Windows setup

```powershell
cd dev-research-agent
py -m venv venv
venv\Scripts\Activate.ps1        # or venv\Scripts\activate.bat in cmd.exe
pip install -r requirements.txt
```

Install ffmpeg (`winget install ffmpeg` or `choco install ffmpeg`), and
Node.js via the [official installer](https://nodejs.org) — it adds `npm`/
`npx` to the **system PATH** automatically, which matters later (see the
MCP note below).

```powershell
cd ..\motion
npm install
cd ..\dev-research-agent
```

Create `dev-research-agent\.env` — same contents as the macOS section above.

### Manual edits required on Windows

The code was developed and tested on macOS/Apple Silicon; three spots are
currently hardcoded for that and need a one-line edit before anything will
run on Windows:

1. **`voiceover.py`** — `generate_voiceover(..., device: str = "mps")`.
   MPS is Apple Silicon-only. Change the default to `"cpu"` (works
   everywhere), or `"cuda"` if you have an NVIDIA GPU with a CUDA-enabled
   PyTorch install.

2. **`render.py`** — `REMOTION_PROJECT = Path("/Users/adityakumar/desktop/motion")`.
   Change to your actual Windows path to the sibling `motion/` folder,
   e.g. `Path(r"C:\Users\you\dev\motion")`.

3. **`render.py`**, inside `_run_remotion_render()` (shared by both
   `render_from_script_and_audio()` and `render_shots_to_video()`, i.e.
   the MCP `render_video` and `render_video_with_shots` tools) — the
   subprocess call shells out via `["zsh", "-lc", cmd]`. That workaround
   exists specifically because macOS's nvm only loads through `.zprofile`
   on a *login* shell, which a GUI-launched process (Claude Desktop) skips
   entirely. Windows doesn't have `zsh`, and Node's official Windows
   installer puts `npx` on the system PATH directly (visible to GUI
   processes without a login-shell workaround), so this needs to become a
   plain subprocess call instead:
   ```python
   result = subprocess.run(
       ["npx", "remotion", "render", composition_id, str(out_path), f"--props={props_path}"],
       cwd=REMOTION_PROJECT,
       capture_output=True,
       text=True,
   )
   ```
   (This only affects the two MCP render tools. The original batch
   `render_video(topic_result)` already calls `npx` directly and doesn't
   need this change.)

**Test the batch pipeline:**

```powershell
python main.py "A quick test topic" --no-render
```

There's no Windows equivalent of the `videogen` bash wrapper — just run
`python main.py "topic"` directly from an activated venv, or reference
`venv\Scripts\python.exe main.py "topic"` from anywhere.

---

## Claude Desktop — MCP tools setup (both platforms)

This is what makes `search_dev_topic`, `generate_voiceover`,
`render_video`, and `render_video_with_shots` available as tools Claude
can call directly in conversation. **Claude Desktop only** — the stdio
transport spawns a local subprocess, which claude.ai (web) and the mobile
apps can't do.

- `render_video` — flat script text, auto-split into sentence chunks.
  Good default for a straightforward explainer.
- `render_video_with_shots` — for content with distinct beats and code
  overlays (a coding-tips reel: hook, method + snippet, method + snippet,
  trap + snippet, CTA). Each shot is `{text, code?, language?, start, end}`
  — `start`/`end` set relative pacing, not literal timestamps (see
  CLAUDE.md for why). Generate the voiceover from the shots' narration
  text first, then pass the real audio path here.

**`generate_voiceover`, `render_video`, and `render_video_with_shots` are
async** — they return a `job_id` immediately, not the finished result. Poll
`check_job_status(job_id)` (returns `"running"`, `"done: <path>"`, or
`"error: <message>"`) every ~15-20s until it's done. This isn't optional —
without it, these tools reliably hit MCP's per-call timeout on realistic
content (confirmed: a real call errored with `MCP error -32001: Request
timed out` after several minutes, while the server kept working and
finished successfully ~10 minutes later in the background). You don't need
to manage this yourself in conversation — just ask Claude to generate the
voiceover/render the video, and it calls `check_job_status` on your behalf
until the job's done.

1. Find your config file:
   - **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
   - **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

2. Add an entry under `"mcpServers"` (merge into the existing object —
   don't replace the whole file):

   **macOS:**
   ```json
   {
     "mcpServers": {
       "dev-video-pipeline": {
         "command": "/Users/you/path/to/dev-research-agent/venv/bin/python3",
         "args": ["/Users/you/path/to/dev-research-agent/mcp_server.py"]
       }
     }
   }
   ```

   **Windows:**
   ```json
   {
     "mcpServers": {
       "dev-video-pipeline": {
         "command": "C:\\Users\\you\\path\\to\\dev-research-agent\\venv\\Scripts\\python.exe",
         "args": ["C:\\Users\\you\\path\\to\\dev-research-agent\\mcp_server.py"]
       }
     }
   }
   ```
   (Windows JSON needs double backslashes, or use forward slashes — both work.)

3. **Fully restart Claude Desktop** (quit the app, not just close the
   window) — it only reads this config on startup.

4. In a new conversation, ask Claude to search for a dev topic — if the
   tool call succeeds, the server is wired up correctly.

---

## Local web app (both platforms)

The simplest, most reliable path to a voiceover — no Claude, no MCP, just
a browser talking to a local Flask process:

```bash
cd dev-research-agent
python3 webapp.py
```

or, if you've symlinked the launcher onto PATH (macOS/Linux, same pattern
as `videogen`):

```bash
chmod +x voiceover-app
ln -sf "$(pwd)/voiceover-app" ~/.local/bin/voiceover-app
voiceover-app
```

Opens `http://127.0.0.1:5959` in your default browser automatically.
Paste a finished script, optionally change the reference voice filename
(default `aditya-voice.m4a`), click **Generate voiceover**. First
generation in a session includes the model-load cost (~90-120s observed);
later generations in the same running session are much faster (~20s
observed) since the model stays loaded in memory. Result plays back
in-page with a download link.

Windows: same `python3 webapp.py` (or `python webapp.py`) — no
platform-specific edits needed here, since this path never shells out to
`npx`/Remotion at all, so the Windows manual-edit notes above don't apply.

---

## Known gaps

- **Windows path is docs-only, not yet tested.** The three edits above are
  what the code needs to run on Windows, based on reading the code, not a
  verified Windows run — the macOS path has been tested end to end
  (including under a stripped/GUI-launch-like environment), Windows has not.
