"""Per-call cost/token logging for every LLM request.

litellm computes an estimated USD cost per response in
``response._hidden_params["response_cost"]`` for known models (OpenRouter and
direct Anthropic both supported). We log it per call and keep a running
session total so cost is visible in the logs instead of only on the provider
dashboard.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("jarvis.usage")

# Running totals for the life of the process. Reset on restart.
_totals: dict[str, float] = {
    "requests": 0.0,
    "cost": 0.0,
    "input_tokens": 0.0,
    "output_tokens": 0.0,
}


def log_completion(response: Any, agent: str) -> None:
    """Log model, token counts, and estimated cost for one completion."""
    try:
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None) if usage else None
        out_tok = getattr(usage, "completion_tokens", None) if usage else None

        hidden = getattr(response, "_hidden_params", None) or {}
        cost = hidden.get("response_cost")
        model = getattr(response, "model", "?")

        _totals["requests"] += 1
        if isinstance(cost, (int, float)):
            _totals["cost"] += cost
        if isinstance(in_tok, int):
            _totals["input_tokens"] += in_tok
        if isinstance(out_tok, int):
            _totals["output_tokens"] += out_tok

        cost_str = f"${cost:.5f}" if isinstance(cost, (int, float)) else "n/a"
        logger.info(
            "cost agent=%s model=%s in=%s out=%s cost=%s | session: %d reqs $%.4f",
            agent,
            model,
            in_tok,
            out_tok,
            cost_str,
            int(_totals["requests"]),
            _totals["cost"],
        )
    except Exception as exc:  # never let cost logging break a response
        logger.debug("cost logging failed: %s", exc)


def session_totals() -> dict[str, float]:
    """Return a copy of the running totals (requests, cost, tokens)."""
    return dict(_totals)
