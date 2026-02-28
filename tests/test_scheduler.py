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
    """insight_poll passes home state summary to triage_agent_fn."""
    from jarvis.scheduler import build_scheduler

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[])
    mock_ha.get_state_summary = MagicMock(return_value="sensor.temp: 20")

    triage_fn = AsyncMock()
    send_fn = AsyncMock()

    scheduler = build_scheduler(mock_ha, triage_fn, None, send_fn)

    # Manually trigger insight_poll without starting the scheduler clock
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    triage_fn.assert_awaited_once()
    call_args = triage_fn.call_args[0][0]
    assert "20" in call_args  # state summary was passed
