"""Per-call cost/token logging for every LLM request.

litellm computes an estimated USD cost per response in
``response._hidden_params["response_cost"]`` for known models (OpenRouter and
direct Anthropic both supported). We log it per call, keep a running
in-process session total, and persist a restart-safe lifetime total (plus a
per-day breakdown for month-to-date figures) to disk — see ``_state_path()``.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import zoneinfo
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.usage")

# Running totals for the life of the process. Reset on restart. For totals that
# survive a restart, see lifetime_totals() / month_to_date_totals() below.
_totals: dict[str, float] = {
    "requests": 0.0,
    "cost": 0.0,
    "input_tokens": 0.0,
    "output_tokens": 0.0,
}

_EMPTY_BUCKET: dict[str, float] = {
    "requests": 0.0, "cost": 0.0, "input_tokens": 0.0, "output_tokens": 0.0,
}


def _state_path() -> Path:
    raw = os.environ.get("USAGE_STATE_PATH", "")
    return Path(raw) if raw else (Path(__file__).parent / "usage_state.json")


def _load_state() -> dict:
    """{'lifetime': {...}, 'daily': {'YYYY-MM-DD': {...}, ...}}, defaulting any
    missing/unreadable piece so callers never have to guard for absent keys."""
    try:
        data = json.loads(_state_path().read_text())
        if not isinstance(data, dict):
            raise ValueError("usage state is not a dict")
    except Exception:
        data = {}
    data.setdefault("lifetime", dict(_EMPTY_BUCKET))
    data.setdefault("daily", {})
    return data


def _save_state(state: dict) -> None:
    """Best-effort persist; never raise (usage tracking must not break a response)."""
    try:
        _state_path().write_text(json.dumps(state))
    except Exception as e:
        logger.debug(f"usage: could not save state: {e}")


def _today() -> str:
    try:
        from jarvis.config import config
        tz = zoneinfo.ZoneInfo(config.TIMEZONE) if config.TIMEZONE else datetime.timezone.utc
    except Exception:
        tz = datetime.timezone.utc
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def _bump(bucket: dict, cost: Any, in_tok: Any, out_tok: Any) -> None:
    bucket["requests"] += 1
    if isinstance(cost, (int, float)):
        bucket["cost"] += cost
    if isinstance(in_tok, int):
        bucket["input_tokens"] += in_tok
    if isinstance(out_tok, int):
        bucket["output_tokens"] += out_tok


def log_completion(response: Any, agent: str) -> None:
    """Log model, token counts, and estimated cost for one completion, and fold it
    into the in-memory session total plus the on-disk lifetime/daily totals."""
    try:
        usage = getattr(response, "usage", None)
        in_tok = getattr(usage, "prompt_tokens", None) if usage else None
        out_tok = getattr(usage, "completion_tokens", None) if usage else None
        cache_read = getattr(usage, "cache_read_input_tokens", None) if usage else None

        hidden = getattr(response, "_hidden_params", None) or {}
        cost = hidden.get("response_cost")
        model = getattr(response, "model", "?")

        _bump(_totals, cost, in_tok, out_tok)

        state = _load_state()
        _bump(state["lifetime"], cost, in_tok, out_tok)
        day_bucket = state["daily"].setdefault(_today(), dict(_EMPTY_BUCKET))
        _bump(day_bucket, cost, in_tok, out_tok)
        _save_state(state)

        cost_str = f"${cost:.5f}" if isinstance(cost, (int, float)) else "n/a"
        logger.info(
            "cost agent=%s model=%s in=%s out=%s cache_read=%s cost=%s | "
            "session: %d reqs $%.4f | lifetime: %d reqs $%.4f",
            agent,
            model,
            in_tok,
            out_tok,
            cache_read,
            cost_str,
            int(_totals["requests"]),
            _totals["cost"],
            int(state["lifetime"]["requests"]),
            state["lifetime"]["cost"],
        )
    except Exception as exc:  # never let cost logging break a response
        logger.debug("cost logging failed: %s", exc)


def session_totals() -> dict[str, float]:
    """Running totals since this process started. Resets on restart."""
    return dict(_totals)


def lifetime_totals() -> dict[str, float]:
    """Persisted all-time totals. Survives restarts."""
    return dict(_load_state()["lifetime"])


def month_to_date_totals(prefix: str | None = None) -> dict[str, float]:
    """Sum of daily buckets whose date starts with ``prefix`` (a 'YYYY-MM' string,
    defaulting to the current month)."""
    prefix = prefix or _today()[:7]
    state = _load_state()
    out = dict(_EMPTY_BUCKET)
    for day, bucket in state["daily"].items():
        if day.startswith(prefix):
            for k in out:
                out[k] += bucket.get(k, 0)
    return out


def today_totals() -> dict[str, float]:
    """Today's bucket (local date), or an all-zero bucket if nothing logged yet."""
    return dict(_load_state()["daily"].get(_today(), _EMPTY_BUCKET))
