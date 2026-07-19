# agent.py
#
# A research -> draft -> critique -> revise pipeline for short-form dev content.
# Runs on Gemini by default, with automatic fallback to a local Ollama model
# if Gemini fails (rate limits, 503s, network issues, etc).
import time
import os
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_ollama import ChatOllama
from langchain_tavily import TavilySearch

load_dotenv()


# ─────────────────────────────────────────────────────────────
# TOOLS
# ─────────────────────────────────────────────────────────────

# Tavily's full schema exposes ~9 optional parameters (time_range, search_depth,
# include_domains, etc.) with constraints that trip up smaller models — e.g.
# time_range and start_date/end_date can't be set together. Wrapping it in a
# narrow tool that only exposes `query` sidesteps that entirely: fewer fields
# means fewer ways for any model (especially a local one) to go off the rails.
_tavily = TavilySearch(max_results=5)

@tool
def search_web(query: str) -> str:
    """Search the web for current, accurate information about a technical topic."""
    return _tavily.invoke({"query": query})


# ─────────────────────────────────────────────────────────────
# MODEL PROVIDERS
# ─────────────────────────────────────────────────────────────

def get_model_for(provider: str):
    """Build a model client for the given provider name ('gemini', 'ollama', or 'groq')."""
    if provider == "ollama":
        model_name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        return ChatOllama(
            model=model_name,
            temperature=0,
            timeout=30,       # fail fast instead of hanging on a stuck connection
            max_retries=2,
        )
    if provider == "groq":
        # Free tier, cloud-hosted (no local app to keep running unlike
        # Ollama), added specifically because Ollama-not-running + a
        # Gemini 503 (Google's servers overloaded, not a quota issue) can
        # both fail at once — a real observed failure, not hypothetical.
        model_name = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        return ChatGroq(
            model=model_name,
            temperature=0,
            timeout=30,
            max_retries=2,
        )
    # default: gemini
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0,
        timeout=30,           # same reasoning — a stuck request should error, not hang
        max_retries=2,
    )


# ─────────────────────────────────────────────────────────────
# SPECIALIST AGENTS
#
# Each of these is a factory function (make_x(model)) rather than a single
# pre-built agent, because invoke_with_fallback() below needs to construct a
# fresh agent bound to whichever model (gemini or ollama) it's currently
# trying. The system prompts all explicitly say "your FINAL message must
# contain X" — without that, a sub-agent will often call its tool, get a
# result, and then hand back a content-free confirmation like "I've
# completed the research" instead of the actual findings.
# ─────────────────────────────────────────────────────────────

def make_researcher(model):
    return create_agent(
        model=model,
        tools=[search_web],
        system_prompt=(
            "You are a research specialist for a developer content creator. "
            "Search for current, accurate information about the given topic. "
            "Your FINAL message must contain the actual findings — key facts "
            "and source URLs — not just a note that you searched."
        ),
    )

def make_writer(model):
    return create_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are a short-form video scriptwriter for a developer content "
            "channel. Given research notes, write a punchy 30-45 second script: "
            "a one-line hook, 3-4 key points explained simply, and a closing "
            "line. Your FINAL message must contain the complete script."
        ),
    )

def make_critic(model):
    return create_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are an editorial critic for short-form dev content. Check the "
            "draft for technical accuracy, clarity, and whether the hook is "
            "strong enough to stop someone mid-scroll. Your FINAL message must "
            "contain specific, actionable feedback — never just 'looks good.'"
        ),
    )

def make_reviser(model):
    return create_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are a script editor. You will be given a draft script and a "
            "critique of it. Revise the draft to address the critique. Return "
            "ONLY the final, polished script — no commentary, no preamble."
        ),
    )

def make_evaluator(model):
    return create_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are a technical fact-checker reviewing a short-form video script "
            "against the research notes it was based on. Check two things:\n"
            "1. Every factual claim in the script is actually supported by the "
            "research notes — flag anything invented or unsupported.\n"
            "2. Any code snippets are technically correct — check hook signatures, "
            "return types, and API usage against what you know of the library.\n\n"
            "Respond in EXACTLY this format:\n"
            "VERDICT: PASS or FAIL\n"
            "ISSUES: a bullet list of specific problems, or 'None' if it passes.\n\n"
            "Be strict — a script with one subtly wrong code example should FAIL "
            "even if everything else is good."
        ),
    )

def make_asset_analyst(model):
    """
    Factory: returns an agent that reads a finished script and outputs a JSON
    array of per-shot visual keyword queries for the Pexels asset fetch.

    One query per shot: {shot_index, keyword (≤4 words), asset_type}.
    Returns ONLY valid JSON — no markdown, no preamble. The asset_hunter
    module strips fenced code block wrappers defensively, but the prompt
    explicitly discourages them.
    """
    return create_agent(
        model=model,
        tools=[],
        system_prompt=(
            "You are a visual asset coordinator for short-form Instagram Reels "
            "about developer topics.\n\n"
            "Given a video script, break it into shots (one per distinct point or "
            "sentence group) and for EACH shot produce ONE short Pexels search "
            "keyword that would find a visually relevant background image or video.\n\n"
            "Rules:\n"
            "- Keep each keyword under 4 words — shorter queries get better results.\n"
            "- For coding/technical points: 'developer typing code', 'laptop dark screen'.\n"
            "- For conceptual points: 'fast loading app', 'smooth animation'.\n"
            "- NEVER use framework names (React, Vue, etc.) as keywords — Pexels has no "
            "relevant results for those.\n"
            "- asset_type must be either 'photo' or 'video'.\n\n"
            "Your FINAL message must be ONLY a valid JSON array, no markdown, no "
            "preamble. Example:\n"
            '[{"shot_index":0,"keyword":"developer typing code","asset_type":"video"},'
            '{"shot_index":1,"keyword":"smooth app animation","asset_type":"video"}]'
        ),
    )

# ─────────────────────────────────────────────────────────────
# FALLBACK WRAPPER
#
# Tries `primary` first; if it raises (rate limit, 503, timeout, anything),
# logs it and retries the same step on `fallback`. Only kicks in on actual
# failure — a normal run never touches the fallback path.
# ─────────────────────────────────────────────────────────────

def to_text(content) -> str:
    """Normalize a message's .content into a plain string.

    AIMessage.content is typed as str | list[str | dict] — usually a plain
    string, but some responses come back as a list of chunks instead. Code
    downstream that assumes a string (.strip(), slicing, regex) will break
    the moment that happens, often confusingly since it looks like a string
    everywhere else.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for chunk in content:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                parts.append(chunk.get("text", str(chunk)))
        return "\n".join(parts)
    return str(content)

def invoke_with_fallback(
    make_agent_fn, content: str, primary="gemini", fallback="ollama",
    fallback2: str | None = None, tracker=None, step_name="",
) -> str:
    """fallback2 is optional and additive — every existing call site keeps
    its original 2-provider chain unless it explicitly opts into a third
    (e.g. asset_hunter.py's keyword extraction uses ollama -> groq ->
    gemini, added after a real run where Ollama wasn't running AND
    Gemini's fallback returned a 503 (server overload), failing both
    providers in the default 2-chain at once)."""
    providers_to_try = [primary, fallback]
    if fallback2:
        providers_to_try.append(fallback2)
    last_error = None

    for provider in providers_to_try:
        start = time.time()
        try:
            agent = make_agent_fn(get_model_for(provider))
            result = agent.invoke({"messages": [{"role": "user", "content": content}]})
            duration = time.time() - start
            # Sum token usage across every AI message in this step — a single
            # agent.invoke() can involve several LLM calls internally (tool
            # calls, retries), each reporting its own usage_metadata.
            input_tokens = output_tokens = 0
            for m in result["messages"]:
                usage = getattr(m, "usage_metadata", None)
                if usage:
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)

            if tracker:
                tracker.record(step_name, provider, duration, input_tokens, output_tokens, success=True)
            return to_text(result["messages"][-1].content)
        except Exception as e:
            if tracker:
                tracker.record(step_name, provider, time.time() - start, 0, 0, success=False)
            last_error = e
            print(f"⚠️  {provider} failed ({type(e).__name__}: {e})")

    raise RuntimeError(f"All providers failed. Last error: {last_error}")


# ─────────────────────────────────────────────────────────────
# PIPELINE
#
# Sequencing is fixed (research always precedes drafting, which always
# precedes critique) so it's plain Python control flow, not an LLM deciding
# what to call next. An earlier version let a supervisor agent decide the
# order — it fired all three delegate calls in parallel on the first turn,
# since nothing forced it to wait, and every step downstream got fed
# hallucinated placeholder input instead of real results. When the order
# isn't actually in question, don't hand it to the model.
# ─────────────────────────────────────────────────────────────

def parse_eval(eval_text: str) -> bool:
    """Returns True only on an unambiguous PASS. Anything unclear counts as FAIL —
    when a check is ambiguous, default to the safer branch, not the convenient one."""
    first_line = eval_text.strip().splitlines()[0].upper() if eval_text.strip() else ""
    return "PASS" in first_line and "FAIL" not in first_line


def run_pipeline(topic: str, tracker=None,max_revision_attempts: int = 2) -> str:
    print(f"[1/5] Researching: {topic}")
    research_notes = invoke_with_fallback(make_researcher, topic,tracker=tracker)

    print("[2/5] Drafting script...")
    draft = invoke_with_fallback(make_writer, research_notes,tracker=tracker)

    print("[3/5] Critiquing draft...")
    critique = invoke_with_fallback(make_critic, draft,tracker=tracker)

    print("[4/5] Revising based on feedback...")
    script = invoke_with_fallback(make_reviser, f"DRAFT:\n{draft}\n\nCRITIQUE:\n{critique}",tracker=tracker)

    print("[5/5] Fact-checking final script against research...")
    eval_text = ""
    for attempt in range(1, max_revision_attempts + 1):
        eval_text = invoke_with_fallback(
            make_evaluator,
            f"RESEARCH NOTES:\n{research_notes}\n\nSCRIPT TO CHECK:\n{script}",
            tracker=tracker
        )
        if parse_eval(eval_text):
            print(f"    ✅ Passed fact-check (attempt {attempt})")
            return script

        print(f"    ⚠️  Failed fact-check (attempt {attempt}):\n{eval_text}\n")
        if attempt < max_revision_attempts:
            print("    Regenerating script to address issues...")
            script = invoke_with_fallback(
                make_reviser, f"DRAFT:\n{script}\n\nCRITIQUE:\n{eval_text}",tracker=tracker
            )

    # Bounded, same principle as recursion_limit back in stage 3 — don't loop
    # forever chasing a perfect pass. After max attempts, surface it honestly
    # instead of either silently shipping or hanging indefinitely.
    print("    ❌ Still failing after retries — flagging for human review.")
    return (
        f"⚠️ NEEDS HUMAN REVIEW — automated fact-check failed after "
        f"{max_revision_attempts} attempts.\n\n{script}\n\n"
        f"--- LAST EVAL FEEDBACK ---\n{eval_text}"
    )


if __name__ == "__main__":
    script = run_pipeline("React's useTransition hook")
    print("\n=== FINAL SCRIPT ===\n")
    print(script)