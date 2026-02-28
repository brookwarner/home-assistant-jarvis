import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN","t"),("TELEGRAM_CHAT_ID","1"),
                 ("HA_TOKEN","h"),("ANTHROPIC_API_KEY","sk")]:
        monkeypatch.setenv(k, v)

async def test_check_user_alerts_fires_when_above_threshold(tmp_path, monkeypatch):
    from jarvis.scheduler import check_user_alerts

    alerts = [
        {"id": "1", "entity_id": "sensor.attic_temp", "condition": "above",
         "threshold": 35.0, "message": "Attic hot!", "enabled": True}
    ]
    alerts_file = tmp_path / "user_alerts.json"
    alerts_file.write_text(json.dumps(alerts))

    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "38.0", "attributes": {}})

    triggered = []
    async def on_trigger(msg):
        triggered.append(msg)

    await check_user_alerts(mock_ha, on_trigger, alerts_path=str(alerts_file))
    assert len(triggered) == 1
    assert "Attic hot!" in triggered[0]

async def test_check_user_alerts_silent_when_below_threshold(tmp_path):
    from jarvis.scheduler import check_user_alerts

    alerts = [
        {"id": "1", "entity_id": "sensor.attic_temp", "condition": "above",
         "threshold": 35.0, "message": "Attic hot!", "enabled": True}
    ]
    alerts_file = tmp_path / "user_alerts.json"
    alerts_file.write_text(json.dumps(alerts))

    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "28.0", "attributes": {}})

    triggered = []
    await check_user_alerts(mock_ha, lambda m: triggered.append(m), alerts_path=str(alerts_file))
    assert len(triggered) == 0


async def test_insight_poll_calls_triage_fn():
    """insight_poll passes diff text to triage_agent_fn when state changes."""
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import build_scheduler

    sched_module._last_snapshot = {"sensor.temp": "15"}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.temp", "state": "20"}
    ])

    triage_fn = AsyncMock()
    send_fn = AsyncMock()

    scheduler = build_scheduler(mock_ha, triage_fn, None, send_fn)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    triage_fn.assert_awaited_once()
    call_args = triage_fn.call_args[0][0]
    assert "15 -> 20" in call_args


async def test_insight_poll_skips_when_no_diff():
    """insight_poll does NOT call triage_fn when state hasn't changed."""
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import build_scheduler

    sched_module._last_snapshot = {"sensor.temp": "20"}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.temp", "state": "20"}
    ])

    triage_fn = AsyncMock()
    scheduler = build_scheduler(mock_ha, triage_fn, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    triage_fn.assert_not_awaited()


async def test_insight_poll_calls_triage_on_binary_change():
    """insight_poll calls triage_fn with diff text when binary sensor changes."""
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import build_scheduler

    sched_module._last_snapshot = {"switch.spa": "off"}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "switch.spa", "state": "on"}
    ])

    triage_fn = AsyncMock()
    scheduler = build_scheduler(mock_ha, triage_fn, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    triage_fn.assert_awaited_once()
    assert "off -> on" in triage_fn.call_args[0][0]


async def test_insight_poll_first_run_stores_snapshot():
    """First poll stores snapshot but does not call triage (no baseline)."""
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import build_scheduler

    sched_module._last_snapshot = {}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.temp", "state": "20"}
    ])

    triage_fn = AsyncMock()
    scheduler = build_scheduler(mock_ha, triage_fn, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    triage_fn.assert_not_awaited()
    assert sched_module._last_snapshot == {"sensor.temp": "20"}


def test_compute_state_diff_first_run_returns_empty():
    """First run (empty last_snapshot) returns snapshot but no diff."""
    from jarvis.scheduler import compute_state_diff
    states = [
        {"entity_id": "sensor.temp", "state": "20"},
        {"entity_id": "switch.spa", "state": "on"},
    ]
    snapshot, diff = compute_state_diff(states, {}, domains=["sensor", "switch"])
    assert len(snapshot) == 2
    assert diff == []


def test_compute_state_diff_no_change():
    from jarvis.scheduler import compute_state_diff
    states = [{"entity_id": "sensor.temp", "state": "20"}]
    last = {"sensor.temp": "20"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert diff == []


def test_compute_state_diff_binary_any_change():
    from jarvis.scheduler import compute_state_diff
    states = [{"entity_id": "binary_sensor.door", "state": "on"}]
    last = {"binary_sensor.door": "off"}
    _, diff = compute_state_diff(states, last, domains=["binary_sensor"])
    assert len(diff) == 1
    assert "off -> on" in diff[0]


def test_compute_state_diff_numeric_noise_filtered():
    from jarvis.scheduler import compute_state_diff
    states = [{"entity_id": "sensor.temp", "state": "20.5"}]
    last = {"sensor.temp": "20.3"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert diff == []


def test_compute_state_diff_numeric_large_change():
    from jarvis.scheduler import compute_state_diff
    states = [{"entity_id": "sensor.temp", "state": "35"}]
    last = {"sensor.temp": "20"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert len(diff) == 1
    assert "20 -> 35" in diff[0]


def test_compute_state_diff_unavailable_transition():
    from jarvis.scheduler import compute_state_diff
    states = [{"entity_id": "sensor.temp", "state": "unavailable"}]
    last = {"sensor.temp": "20"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert len(diff) == 1


def test_compute_state_diff_new_entity():
    from jarvis.scheduler import compute_state_diff
    states = [
        {"entity_id": "sensor.temp", "state": "20"},
        {"entity_id": "sensor.humidity", "state": "65"},
    ]
    last = {"sensor.temp": "20"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert len(diff) == 1
    assert "new" in diff[0].lower()


def test_compute_state_diff_entity_removed():
    from jarvis.scheduler import compute_state_diff
    states = [{"entity_id": "sensor.temp", "state": "20"}]
    last = {"sensor.temp": "20", "sensor.humidity": "65"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert len(diff) == 1
    assert "removed" in diff[0].lower()


def test_compute_state_diff_domain_filter():
    from jarvis.scheduler import compute_state_diff
    states = [
        {"entity_id": "sensor.temp", "state": "20"},
        {"entity_id": "light.kitchen", "state": "on"},
    ]
    snapshot, _ = compute_state_diff(states, {}, domains=["sensor"])
    assert "light.kitchen" not in snapshot


def test_compute_state_diff_pct_threshold():
    """Small absolute change that exceeds pct threshold IS reported."""
    from jarvis.scheduler import compute_state_diff
    # 0.5->1.0: abs=0.5 (below 2.0) but pct=100% (above 5%) => reported
    states = [{"entity_id": "sensor.power", "state": "1.0"}]
    last = {"sensor.power": "0.5"}
    _, diff = compute_state_diff(states, last, domains=["sensor"])
    assert len(diff) == 1
