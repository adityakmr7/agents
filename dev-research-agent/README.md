# Dev Content Pipeline — Setup Guide

Turns a topic into a finished vertical (1080×1920) video: researched
script, cloned-voice narration, rendered motion-graphic captions. Two
sibling projects, two ways to run it — see [CLAUDE.md](CLAUDE.md) for the
full architecture and known gotchas; this doc is just setup.

```
parent-folder/
  dev-research-agent/   <- this project (Python)
  motion/                <- Remotion renderer (TypeScript/React)
```

## Two ways to use it

| | Batch pipeline | Interactive MCP tools |
|---|---|---|
| **Entry point** | `videogen` CLI / `main.py` | Claude Desktop |
| **Who drafts/critiques the script** | A second LLM (Gemini/Ollama), unattended | Claude, live in conversation |
| **Use when** | Scripted/unattended runs | You're already talking to Claude Desktop |

Both share the same mechanical steps underneath (search, TTS, render), so
most of this setup applies to either one.

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
  Note: the code's default filename is `my_voice.wav`, but that file may
  not actually exist in your checkout — either add one, or pass
  `reference_path` explicitly (see "Known gaps" below).

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

3. **`render.py`**, inside `render_from_script_and_audio()` — the
   subprocess call shells out via `["zsh", "-lc", cmd]`. That workaround
   exists specifically because macOS's nvm only loads through `.zprofile`
   on a *login* shell, which a GUI-launched process (Claude Desktop) skips
   entirely. Windows doesn't have `zsh`, and Node's official Windows
   installer puts `npx` on the system PATH directly (visible to GUI
   processes without a login-shell workaround), so this needs to become a
   plain subprocess call instead:
   ```python
   result = subprocess.run(
       ["npx", "remotion", "render", COMPOSITION_ID, str(out_path), f"--props={props_path}"],
       cwd=REMOTION_PROJECT,
       capture_output=True,
       text=True,
   )
   ```
   (This only affects the MCP `render_video` tool. The original batch
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

This is what makes `search_dev_topic`, `generate_voiceover`, and
`render_video` available as tools Claude can call directly in
conversation. **Claude Desktop only** — the stdio transport spawns a
local subprocess, which claude.ai (web) and the mobile apps can't do.

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

## Known gaps

- **`my_voice.wav` doesn't exist by default.** Only `osho-voice.mp3` is
  actually in the project (used as the CLI's default reference clip). The
  MCP `generate_voiceover` tool defaults to `my_voice.wav` and will raise
  a clear `FileNotFoundError` if you don't either add that file or pass
  `reference_path` explicitly.
- **Windows path is docs-only, not yet tested.** The three edits above are
  what the code needs to run on Windows, based on reading the code, not a
  verified Windows run — the macOS path has been tested end to end
  (including under a stripped/GUI-launch-like environment), Windows has not.
