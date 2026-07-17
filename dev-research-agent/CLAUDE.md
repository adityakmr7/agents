# Dev Content Pipeline — Project Context

This assumes you're pointed at a parent folder containing two sibling projects:
- `dev-research-agent/` — Python. Researches a topic, drafts/critiques/revises a
  short-form video script, fact-checks it, and generates a cloned-voice voiceover.
- `motion/` — Remotion (TypeScript/React). Renders the script + voiceover into a
  finished vertical (1080x1920) video.

If your actual folder names or layout differ, update the paths below before relying on this.

## Two entry points — batch pipeline vs. interactive MCP tools

There are now two ways to produce a video. Same underlying mechanics
(Tavily search, Chatterbox TTS, Remotion render), different amount of LLM
reasoning done *inside Python* vs. *in the calling conversation*.

### 1. Fully-automated batch pipeline (`agent.py` / `main.py` / `videogen` CLI)

Use this for unattended/batch runs — a script kicked off from a terminal
(or the `videogen` CLI) with no one watching, or reproducible fully-scripted
generation. `agent.py`'s `run_pipeline()` drives its own
research→draft→critique→revise→fact-check loop by calling out to a second
LLM (Gemini, falling back to Ollama) at every step — necessary because
nothing else is "in the loop" to do that reasoning.

1. `cd dev-research-agent && python3 main.py "topic"` (or `videogen "topic"`
   from anywhere, or call `create_video_assets(topic)` directly) — runs the
   full pipeline, saves `output/<topic-slug>/{script.txt, voiceover.wav, run_log.json}`.
2. `render_video(result)` from `render.py` — copies the voiceover into
   `motion/public/`, cleans the script for on-screen text, writes a props file,
   and runs `npx remotion render` from inside `motion/`.
3. Final output: `dev-research-agent/output/<topic-slug>/video.mp4`

Ask before running step 2 if it hasn't been confirmed the script/voiceover from
step 1 actually sound right — rendering is the slow step, not worth doing on
output that's about to be redone anyway.

### 2. Interactive MCP tools (`mcp_server.py`) — via Claude Desktop

Use this when a human (via Claude Desktop) is already in the conversation
doing the research synthesis, drafting, critiquing, and revising directly —
having Gemini/Ollama do that same reasoning a second time inside Python
would just be a redundant, slower, lower-quality LLM-in-the-loop for
something Claude is already doing live. `mcp_server.py` exposes exactly
the three MECHANICAL steps as MCP tools, with **no LLM reasoning inside any
of them**:

- `search_dev_topic(query)` — the same narrow Tavily wrapper `agent.py`
  uses (`_tavily`), called directly with no agent/LLM wrapping it. Returns
  raw results for Claude to read and synthesize itself.
- `generate_voiceover(script_text, reference_path="my_voice.wav")` — same
  `clean_script_for_voiceover()` + chunking + Chatterbox/MPS call as
  `voiceover.py`. Pass Claude's own finished script text.
- `render_video(script_text, audio_path)` — same copy/props/`npx remotion
  render` steps as `render.py`, via the new `render_from_script_and_audio()`
  (kept separate from the existing `render_video(topic_result)` so the
  batch pipeline's entry point is untouched).

`agent.py`'s `create_agent`/provider-fallback/tool-schema-narrowing
machinery is intentionally NOT used here — that complexity exists to make
an *external* model behave reliably unattended; it's dead weight when
Claude Desktop is the one already reasoning in the conversation.

**Claude Desktop only** — the stdio transport this server uses spawns a
local subprocess, which claude.ai (web) and the mobile apps can't do; only
the Desktop app hosts local stdio MCP servers. Registration goes in
`~/Library/Application Support/Claude/claude_desktop_config.json` under
`mcpServers`; Desktop needs a restart to pick up config changes.

**Known gap:** the default `reference_path="my_voice.wav"` doesn't
actually exist in this project — only `osho-voice.mp3` does (same
discrepancy the earlier CLAUDE.md draft had). Pass `reference_path`
explicitly, or add a real `my_voice.wav` clip to the project root.

**Paths:** every path in `mcp_server.py` is resolved relative to the
file's own directory (`PROJECT_ROOT`), never the process's cwd — Claude
Desktop launches this server with its own working directory, not
`dev-research-agent/`. `.env` is pre-loaded by absolute path for the same
reason (`agent.py`'s own bare `load_dotenv()` can't find it otherwise;
confirmed by reproducing the failure before fixing it).

**`npx`/nvm:** Claude Desktop is a GUI app launched by `launchd`, which
does NOT source `.zprofile` — so `npx` (installed via nvm, not on the
system PATH) is invisible to a bare subprocess env. Confirmed:
`env -i which npx` fails; `zsh -lc 'which npx'` succeeds (nvm loads via
`.zprofile` on a login shell). `render_from_script_and_audio()` in
`render.py` shells out via `zsh -lc "npx remotion render ..."` specifically
so this works regardless of the parent process's PATH — verified end to
end under a fully stripped (`env -i`) environment. The original
`render_video(topic_result)` still calls `npx` directly (untouched, still
correct for its existing terminal-invoked call sites) — if it's ever
wired into something else GUI-launched, it'll need the same fix.

## Environment

- Mac, Apple Silicon, 16GB RAM. Python 3.14 (Homebrew) — unusually new, several
  gotchas below trace back to this specifically.
- `dev-research-agent` has its own venv — activate it before running anything Python.
- `.env` in `dev-research-agent/`: `GOOGLE_API_KEY`, `TAVILY_API_KEY`, `OLLAMA_MODEL`
  (currently `llama3.1:8b`). Ollama must be running locally for the fallback path
  to work at all.
- Voice cloning reference clip: intended to be `my_voice.wav` (~5s clean
  single-speaker audio), but that file doesn't currently exist in the
  project — `osho-voice.mp3` is what's actually present and used as the
  default in `main.py`/`voiceover.py`. See the MCP tools section below.
- No deployment — this all runs locally, for personal use only. Don't suggest
  Docker/cloud hosting unless explicitly asked again.

## Known gotchas — read before "fixing" something that isn't actually broken

**LangChain/agents**
- Use `create_agent` from `langchain.agents`. NOT `create_react_agent` /
  `AgentExecutor` — deprecated, will import-error. If a fix looks like it matches
  an old tutorial, verify against current docs before applying it.
- Every model client needs `timeout=30, max_retries=2`. Without it, a `503` from
  Gemini can retry silently forever and look exactly like a hang — this cost real
  debugging time once already.
- `gemini-3.5-flash` free tier: **20 requests/day**, not per-minute. One full
  pipeline run costs ~4-6 calls. Don't iterate/test against Gemini-primary — use
  `primary="ollama"` for anything repetitive, save real Gemini calls for actual runs.
- Tool schemas should stay narrow. `TavilySearch`'s full ~9-param schema trips up
  smaller/local models (mutually exclusive params, wrong types); wrapped down to
  `query: str` only, it's reliable.
- Pipeline steps with a genuinely fixed order (research → draft → critique →
  revise) are plain sequential Python calls, NOT an LLM-driven supervisor deciding
  call order. A supervisor version of this fired all delegate tool calls in
  parallel and fed downstream steps hallucinated placeholder input. Only let the
  model decide sequencing when the order is actually uncertain.
- A message's `.content` can be `str` OR `list[str | dict]` depending on
  provider/response shape. Always pass it through the `to_text()` helper in
  `agent.py` before calling string methods on it.

**Python 3.14 / dependency chain (Chatterbox TTS)**
- `resemble-perth` (Chatterbox's watermarking dep) needs `pkg_resources`, which
  needs `setuptools<82` — v82.0.0 (Feb 2026) removed `pkg_resources` entirely.
  Must be pinned in `requirements.txt`, not just installed once.
- `torchaudio.save()` requires the separate `torchcodec` package (torchaudio 2.9+
  changed this). May also need `brew install ffmpeg` if torchcodec itself errors.
- Chatterbox works on `device="mps"` on this machine — confirmed, no CPU fallback
  needed. If it ever fails on `mps`, fall back to `"cpu"` rather than assuming MPS
  is broken outright.

**Voiceover generation**
- Chatterbox has a fixed sampling-step budget per `generate()` call — long scripts
  must be chunked at sentence boundaries (`split_into_chunks`), not sent whole.
- Script text MUST go through `clean_script_for_voiceover()` before TTS — strips
  markdown (`**bold**`, `` `code` ``) and stage directions (parenthetical lines)
  that would otherwise get read aloud literally.

**Remotion**
- Audio must live inside `motion/public/` — `staticFile()` can't reach outside
  the project, so `render.py` copies the voiceover in before rendering.
- Use `parseMedia()` from `@remotion/media-parser` for dynamic duration
  (`calculateMetadata`), not the deprecated `getAudioDurationInSeconds()`.
- Pass render props via a JSON file (`--props=./file.json`), not inline
  `--props='{...}'` — avoids shell-escaping issues with long script text and
  breaks outright on Windows shells either way.
- On-screen text needs the SAME `clean_script_for_voiceover()` treatment as the
  audio — otherwise literal `**Hook:**` markdown shows up visibly on screen.

## Still open / not yet built

- `evals.py` — planned, not implemented. Should default to Ollama-primary (not
  Gemini) so regression testing doesn't eat the daily quota.
- No LLM-as-judge quality check yet — current fact-check step (in `agent.py`,
  self-built) catches factual mismatches against research, not things like a
  fluent-but-technically-wrong code example unless it happens to contradict the
  research notes directly.
- **Security follow-up**: a real Gemini API key was pasted into a chat session
  during development. Confirm it's been regenerated in Google AI Studio if that
  hasn't happened yet — don't reuse the old one.