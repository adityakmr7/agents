# evals.py
"""
A small, repeatable regression check for the pipeline — not a one-off
manual read-through. Run this after changing a prompt, a schema, or a
library version, and it tells you in ~30 seconds whether anything broke.

Defaults to Ollama-primary deliberately: this tests pipeline *logic*, not
model *quality*, so it shouldn't eat into the scarce Gemini daily quota.
"""

import json
from pathlib import Path
from agent import run_pipeline
from voiceover import clean_script_for_voiceover

# A small, fixed set of topics — enough to catch regressions without being
# slow to run repeatedly. Mix familiar (React hooks) with something new,
# so a check doesn't accidentally pass just because the model has "seen"
# similar output before in this session.
TEST_TOPICS = [
    "React's useTransition hook",
    "React's useDeferredValue hook",
    "TypeScript's satisfies operator",
]


def check_script(script: str) -> dict:
    """Run a set of automated pass/fail checks on a generated script."""
    checks = {}

    checks["nonempty"] = len(script.strip()) > 0
    checks["reasonable_length"] = 200 <= len(script) <= 3000

    cleaned = clean_script_for_voiceover(script)
    checks["cleans_without_erroring"] = True  # would have raised above if not
    checks["no_leftover_markdown"] = "**" not in cleaned and "```" not in cleaned
    checks["no_leftover_backticks"] = "`" not in cleaned

    return checks


def run_evals(primary="ollama", fallback="gemini") -> dict:
    results = []

    for topic in TEST_TOPICS:
        print(f"\n--- Testing: {topic} ---")
        try:
            # NOTE: requires run_pipeline / invoke_with_fallback to accept
            # primary+fallback as parameters, not hardcoded — if yours
            # still hardcodes "gemini"/"ollama" inside run_pipeline, that's
            # a quick signature change needed before this works.
            script = run_pipeline(topic, primary=primary, fallback=fallback)
            checks = check_script(script)
            passed = all(checks.values())
            print(f"  {'✅ PASS' if passed else '❌ FAIL'} — {checks}")
            results.append({"topic": topic, "passed": passed, "checks": checks, "error": None})
        except Exception as e:
            print(f"  ❌ ERROR — {type(e).__name__}: {e}")
            results.append({"topic": topic, "passed": False, "checks": {}, "error": str(e)})

    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    print(f"\n=== EVAL SUMMARY: {passed}/{total} passed ===")

    Path("eval_results.json").write_text(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    run_evals()