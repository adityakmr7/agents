# Dev Content Pipeline — Project Context

This assumes you're pointed at a parent folder containing two sibling projects:
- `dev-research-agent/` — Python. Researches a topic, drafts/critiques/revises a
  short-form video script, fact-checks it, and generates a cloned-voice voiceover.
- `motion/` — Remotion (TypeScript/React). Renders the script + voiceover into a
  finished vertical (1080x1920) video.

If your actual folder names or layout differ, update the paths below before relying on this.

## Three entry points — batch pipeline, MCP tools, local web app

There are now three ways to produce a voiceover/video. Same underlying
mechanics (Tavily search, Chatterbox TTS, Remotion render), different
amount of LLM reasoning done *inside Python* vs. *in a calling
conversation*, and — for the web app — a deliberately simpler surface
that trades away Claude's involvement entirely in exchange for
reliability.

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
these MECHANICAL steps as MCP tools, with **no LLM reasoning inside any
of them**:

- `search_dev_topic(query)` — the same narrow Tavily wrapper `agent.py`
  uses (`_tavily`), called directly with no agent/LLM wrapping it. Returns
  raw results for Claude to read and synthesize itself. Fast, synchronous —
  no job/polling needed.
- `generate_voiceover(script_text, reference_path="aditya-voice.m4a")` — same
  `clean_script_for_voiceover()` + chunking + Chatterbox/MPS call as
  `voiceover.py`. Pass Claude's own finished script text. **Async — see below.**
- `render_video(script_text, audio_path)` — same copy/props/`npx remotion
  render` steps as `render.py`, via `render_from_script_and_audio()`
  (kept separate from the existing `render_video(topic_result)` so the
  batch pipeline's entry point is untouched). Renders through the plain
  `NarratedVideo` composition — flat text auto-split into sentence chunks.
  **Async — see below.**
- `render_video_with_shots(shots, audio_path, topic="")` — for content
  with distinct beats and code overlays (a talking-points reel: hook,
  method + snippet, method + snippet, trap + snippet, CTA), not just flat
  narration. Each `Shot` is `{text, code?, language?, start, end}`. Renders
  through `ScreenplayVideo` instead of `NarratedVideo` — see
  `motion/src/ScreenplayVideo.tsx` and `motion/src/ShotScene.tsx`/`CodeCard.tsx`
  for the composition + code-overlay rendering itself.
  **Important:** a shot's `start`/`end` are the screenwriter's *intended*
  timestamps, used only as a **relative pacing weight** — they are NOT
  wired to literal clock times in the render, because this pipeline has no
  word-level transcription alignment (same documented gap as
  `NarratedVideo.tsx`'s sentence-chunk timing). Generate the voiceover from
  the shots' concatenated narration text first (`generate_voiceover`),
  *then* pass that real audio's path here — the shots' relative durations
  get rescaled to fit however long the actual TTS audio runs. **Async — see below.**

**Async job pattern (`generate_voiceover`/`render_video`/`render_video_with_shots`):**
these three return a **job_id immediately** instead of blocking, and the
actual work runs on a background thread — poll `check_job_status(job_id)`
(returns `"running"`, `"done: <path>"`, or `"error: <message>"`) until it's
no longer `"running"`. This isn't optional/defensive — it's a fix for a
real, reproduced failure: a direct call to `generate_voiceover` (via this
same MCP connection, not hearsay) returned `MCP error -32001: Request timed
out` after the client's own timeout, while the server kept working in the
background and actually finished ~10 minutes later. No amount of "just
wait longer" fixes a hard client-side timeout; the tool call itself has to
return fast regardless of how long the underlying work takes. Jobs are
in-memory only (`_jobs` dict in `mcp_server.py`) — they don't survive a
server restart, which is fine since Claude Desktop only restarts this
process when Desktop itself restarts, at which point the job's
conversation context is gone anyway.

**Chatterbox model caching:** `generate_voiceover` calling
`ChatterboxTTS.from_pretrained()` on every single call (never cached) was
the dominant cost behind that ~10-minute run — confirmed by direct
comparison: a cold call took the model-load time in full, a second call in
the same server process afterward returned instantly once you account for
polling latency. `_get_tts_model()` in `mcp_server.py` now caches the
loaded model at module level (lock-guarded against a lazy-load race), so
only the first `generate_voiceover` call per Claude Desktop session pays
the load cost. `voiceover.py`'s `generate_voiceover()` gained an optional
`model=` param for this — `None` (the default) preserves the exact
existing behavior for the batch CLI, which is always a fresh process
anyway and wouldn't benefit from caching.

**A subtle bug caught while building the async fix, worth knowing if you
touch `_start_job` call sites:** don't pass an eagerly-evaluated function
call as a kwarg to `_start_job` (e.g. `model=_get_tts_model()`) — Python
evaluates call arguments *before* the call happens, so that "kwarg" runs
synchronously in the dispatching thread, not the background one, silently
defeating the entire point of making the tool return immediately. Confirmed
this exact mistake with a timed test (18.7s to "return immediately")
before catching and fixing it — the slow call has to happen *inside* the
function passed to `_start_job`, not in an argument to it.

`agent.py`'s `create_agent`/provider-fallback/tool-schema-narrowing
machinery is intentionally NOT used here — that complexity exists to make
an *external* model behave reliably unattended; it's dead weight when
Claude Desktop is the one already reasoning in the conversation.

**Claude Desktop only** — the stdio transport this server uses spawns a
local subprocess, which claude.ai (web) and the mobile apps can't do; only
the Desktop app hosts local stdio MCP servers. Registration goes in
`~/Library/Application Support/Claude/claude_desktop_config.json` under
`mcpServers`; Desktop needs a restart to pick up config changes.

Default `reference_path` is `aditya-voice.m4a`, matching `main.py`'s CLI
default — that file exists in the project root, so `generate_voiceover()`
works with no `reference_path` argument out of the box.

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
`.zprofile` on a login shell). The shared `_run_remotion_render()` helper
in `render.py` (used by both `render_from_script_and_audio()` and
`render_shots_to_video()`) shells out via `zsh -lc "npx remotion render
..."` specifically so this works regardless of the parent process's PATH —
verified end to end under a fully stripped (`env -i`) environment for both.
The original `render_video(topic_result)` still calls `npx` directly
(untouched, still correct for its existing terminal-invoked call sites) —
if it's ever wired into something else GUI-launched, it'll need the same fix.

### 3. Local web app (`webapp.py`) — "paste a script, get a voiceover"

Use this when you just want a voiceover from a script you already have,
with no Claude in the loop at all. Built specifically because the MCP path
(above) turned out to have a problem no amount of code fixing solves:
Claude Desktop spawns a **separate `mcp_server.py` process per
conversation/tab**, so two conversations both using `dev-video-pipeline`
at once means two independent ~650MB Chatterbox model instances competing
for memory on a 16GB machine — confirmed via system logs (macOS's
`memorystatus` killer actively reclaiming processes) at the exact moment
of a reported failure, with no crash report anywhere (ruling out a
segfault). `webapp.py` sidesteps the entire problem class: it's a single
Flask process, started once, reached from your own browser over plain
HTTP — no MCP stdio transport, no per-conversation process spawning, and
no client-side tool-call timeout the way MCP's RPC has (`fetch()` has no
default timeout).

- Run: `voiceover-app` (symlinked to `~/.local/bin`, same pattern as
  `videogen`) or `python3 webapp.py` from the project root. Opens
  `http://127.0.0.1:5959` in your default browser automatically.
- One page: paste script text, optionally override the reference voice
  filename (default `aditya-voice.m4a`), click Generate. Same
  `generate_voiceover()` + Chatterbox/MPS call as every other entry point,
  with the same model-caching pattern as `mcp_server.py`
  (`_get_tts_model()`) — only the first generation in a running
  `webapp.py` session pays the model-load cost.
- Async under the hood too (`/generate` returns a `job_id`, the page polls
  `/status/<job_id>` every 2s) — not because the browser needs it to avoid
  a timeout, but because it gives the same "still working, Ns elapsed"
  feedback as a blocking request without actually blocking the tab.
- Output: `output/webapp-voiceovers/voiceover-<job_id>.wav`, served back
  to the page via `/audio/<filename>` with a `<audio>` player + download
  link.
- Verified end to end via real browser interaction (Browser pane, not
  just curl): cold generation 115s (model load + synthesis), second
  generation in the same session 20s (cached model, just synthesis).
- Deliberately does NOT do research/drafting/critique (paste a *finished*
  script) or video rendering (voiceover only) — if video rendering via
  this same no-MCP pattern becomes useful, it'd be an additional route on
  this same Flask app calling `render_from_script_and_audio()`, not a
  separate app.

### `clean_script_for_voiceover()` — shot-list screenplay support

Handles a specific real format now (not just the batch pipeline's
markdown-style `**Hook:**` scripts): a "shot list" with bracketed
timestamp+label headers and quoted narration per beat, e.g.:

```
[0:00–0:03] HOOK
"Know just THREE array methods, and 90% of interview questions are solved."

[0:03–0:11] MAP
"One: map. Transform every element, get a new array back."
```

Both the `[H:MM–H:MM] LABEL` header lines and the wrapping quote marks are
stripped before TTS/on-screen text — shared by every entry point (CLI,
MCP tools, web app) since they all funnel through this one function.

**A real regex bug found and fixed while adding this, worth knowing before
touching these patterns again:** the naive fix used `^\s*` for leading
whitespace on each stripped-line pattern. `\s` matches newlines too, so
when a blank separator line preceded a line that regex needed to strip,
`^\s*` — anchored at the *blank line's own start* (MULTILINE `^` matches
there too) — could greedily walk forward through the blank line, past its
trailing newline, and into the next line's content, then match+strip
starting from there. Net effect: the blank line's separating newline gets
consumed as part of the "matched and deleted" text, so once empty lines
are filtered out downstream, two shots that should be space-separated end
up glued together with **zero space** — confirmed with a real paste:
`"...watch till the end.One: map..."`. Same latent bug existed in the
original (pre-dating this session) stage-direction stripper too, just
never triggered because that pattern's typical input didn't happen to
have a blank line immediately before it as often. Fix: `[ \t]*` instead of
`\s*` for any leading/trailing whitespace matcher anchored to `^`/`$` in
this function — restricts it to same-line whitespace, can't reach across
a line boundary. If you add another stripping rule here, use `[ \t]*`, not
`\s*`, for exactly this reason.

**Also worth knowing:** verifying this "worked" against a running
`webapp.py`/`mcp_server.py` process is not enough — Python doesn't
hot-reload. A first attempt at verifying this fix was actually run against
a *stale* process that had been started before the fix landed, silently
testing old code and appearing to pass a shallow check. Caught by
comparing the process's start time (`ps -o lstart`) against the file's
last-edit time (`stat -f "%Sm"`) — always do that comparison before
trusting a live test against a long-running server process here.

## Environment

- Mac, Apple Silicon, 16GB RAM. Python 3.14 (Homebrew) — unusually new, several
  gotchas below trace back to this specifically.
- `dev-research-agent` has its own venv — activate it before running anything Python.
- `.env` in `dev-research-agent/`: `GOOGLE_API_KEY`, `TAVILY_API_KEY`, `OLLAMA_MODEL`
  (currently `llama3.1:8b`). Ollama must be running locally for the fallback path
  to work at all.
- Voice cloning reference clip: `aditya-voice.m4a` (~5s clean
  single-speaker audio) — the default in `main.py` (CLI/batch pipeline)
  and `mcp_server.py` (MCP tools) alike. `osho-voice.mp3` is still in the
  project too (an earlier reference clip) and can be passed explicitly if
  needed, but isn't the default anywhere anymore.
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