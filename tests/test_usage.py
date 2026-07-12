import json
import logging
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from jarvis import usage


@pytest.fixture(autouse=True)
def _isolated_usage_state(tmp_path, monkeypatch):
    """Every test gets its own state file (never the real repo-root usage_state.json)
    and a freshly-zeroed in-process session total, so tests can't see each other's
    calls or touch a file the rest of the working tree cares about."""
    monkeypatch.setenv("USAGE_STATE_PATH", str(tmp_path / "usage_state.json"))
    monkeypatch.setattr(usage, "_totals", dict(usage._EMPTY_BUCKET))
    yield


def _resp(cost=0.001, in_tok=100, out_tok=20, model="anthropic/claude-haiku-4-5"):
    resp = MagicMock()
    resp.usage = MagicMock(
        prompt_tokens=in_tok, completion_tokens=out_tok,
        cache_read_input_tokens=0, cache_creation_input_tokens=0,
    )
    resp._hidden_params = {"response_cost": cost}
    resp.model = model
    return resp


def test_log_completion_records_cache_reads(caplog):
    resp = MagicMock()
    resp.usage = MagicMock(
        prompt_tokens=7000, completion_tokens=40,
        cache_read_input_tokens=6500, cache_creation_input_tokens=0,
    )
    resp._hidden_params = {"response_cost": 0.001}
    resp.model = "anthropic/claude-haiku-4-5"
    with caplog.at_level(logging.INFO, logger="jarvis.usage"):
        usage.log_completion(resp, "conversation")
    assert "cache_read=6500" in caplog.text


def test_log_completion_persists_lifetime_and_daily_totals():
    """The whole point of persisting usage is that it survives a restart — check
    the lifetime/today totals read back from disk, not just the in-memory session."""
    usage.log_completion(_resp(cost=0.001, in_tok=100, out_tok=20), "conversation")
    usage.log_completion(_resp(cost=0.002, in_tok=200, out_tok=40), "briefing")

    lifetime = usage.lifetime_totals()
    assert lifetime["requests"] == 2
    assert round(lifetime["cost"], 5) == 0.003
    assert lifetime["input_tokens"] == 300
    assert lifetime["output_tokens"] == 60

    today = usage.today_totals()
    assert today["requests"] == 2
    assert round(today["cost"], 5) == 0.003


def test_lifetime_totals_persist_across_a_fresh_load(monkeypatch):
    """Persistence must not depend on the in-process _totals dict — a brand new
    process reading the same state file should see the same lifetime figures."""
    usage.log_completion(_resp(cost=0.005), "conversation")

    # Simulate a restart: reset the in-memory session total, keep the file on disk.
    monkeypatch.setattr(usage, "_totals", dict(usage._EMPTY_BUCKET))
    assert usage.session_totals()["requests"] == 0

    lifetime = usage.lifetime_totals()
    assert lifetime["requests"] == 1
    assert round(lifetime["cost"], 5) == 0.005


def test_month_to_date_totals_sums_only_the_given_month():
    state_path = Path(usage._state_path())
    state_path.write_text(json.dumps({
        "lifetime": {"requests": 3, "cost": 0.6, "input_tokens": 300, "output_tokens": 60},
        "daily": {
            "2026-07-01": {"requests": 1, "cost": 0.1, "input_tokens": 100, "output_tokens": 20},
            "2026-07-12": {"requests": 1, "cost": 0.2, "input_tokens": 100, "output_tokens": 20},
            "2026-06-30": {"requests": 1, "cost": 0.3, "input_tokens": 100, "output_tokens": 20},
        },
    }))

    july = usage.month_to_date_totals("2026-07")
    assert july["requests"] == 2
    assert round(july["cost"], 5) == 0.3

    june = usage.month_to_date_totals("2026-06")
    assert june["requests"] == 1
    assert round(june["cost"], 5) == 0.3


def test_lifetime_totals_default_to_zero_when_state_missing():
    lifetime = usage.lifetime_totals()
    assert lifetime == dict(usage._EMPTY_BUCKET)
