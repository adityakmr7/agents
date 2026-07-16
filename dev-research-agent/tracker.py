# tracker.py
"""
Lightweight run-level observability: tracks every LLM call's provider,
duration, and token usage, then reports a summary — call counts, tokens,
and an estimated dollar cost if this were on a paid tier.

This matters more than it sounds like right now: you're not actually being
billed (free tier), but you ARE capped at 20 Gemini requests/day, and this
tracker tells you exactly how many of those a single run consumes — the
thing that actually bit you before with the 429 errors.
"""

import time
import json
from pathlib import Path

# Verified current pricing (per 1M tokens) — check ai.google.dev/gemini-api/docs/pricing
# before trusting this for real budgeting; prices change and this hardcodes a snapshot.
PRICING_PER_1M_TOKENS = {
    "gemini": {"input": 1.50, "output": 9.00},
    "ollama": {"input": 0.0, "output": 0.0},  # local — always free
}


class RunTracker:
    def __init__(self):
        self.steps = []
        self.start_time = time.time()

    def record(self, step_name, provider, duration, input_tokens, output_tokens, success):
        self.steps.append({
            "step": step_name,
            "provider": provider,
            "duration_sec": round(duration, 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "success": success,
        })

    def summary(self) -> dict:
        gemini_calls = sum(1 for s in self.steps if s["provider"] == "gemini" and s["success"])
        ollama_calls = sum(1 for s in self.steps if s["provider"] == "ollama" and s["success"])
        total_input = sum(s["input_tokens"] for s in self.steps)
        total_output = sum(s["output_tokens"] for s in self.steps)

        est_cost = sum(
            (s["input_tokens"] / 1_000_000) * PRICING_PER_1M_TOKENS.get(s["provider"], {"input": 0})["input"]
            + (s["output_tokens"] / 1_000_000) * PRICING_PER_1M_TOKENS.get(s["provider"], {"output": 0})["output"]
            for s in self.steps
        )

        return {
            "total_wall_time_sec": round(time.time() - self.start_time, 2),
            "gemini_calls": gemini_calls,
            "ollama_calls": ollama_calls,
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "estimated_cost_usd_if_paid_tier": round(est_cost, 5),
            "steps": self.steps,
        }

    def print_summary(self):
        s = self.summary()
        print("\n=== RUN SUMMARY ===")
        print(f"Total time: {s['total_wall_time_sec']}s")
        print(f"Gemini calls this run: {s['gemini_calls']}  (daily free-tier cap is 20)")
        print(f"Ollama calls (fallback triggered): {s['ollama_calls']}")
        print(f"Tokens: {s['total_input_tokens']} in / {s['total_output_tokens']} out")
        print(f"Estimated cost on paid tier: ${s['estimated_cost_usd_if_paid_tier']}")

    def save(self, path):
        Path(path).write_text(json.dumps(self.summary(), indent=2))