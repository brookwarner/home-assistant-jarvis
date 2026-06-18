import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN","t"),("TELEGRAM_CHAT_ID","1"),
                 ("HA_TOKEN","h"),("ANTHROPIC_API_KEY","sk")]:
        monkeypatch.setenv(k, v)


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    # The diff snapshot and per-mode cadence gate are module globals; reset between tests.
    from jarvis import scheduler as s
    s._last_snapshot = {}
    s._last_proactive_run = None
    yield

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

    sched_module._last_snapshot = {"sensor.caravan_temperature": "15"}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.caravan_temperature", "state": "20"}
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

    sched_module._last_snapshot = {"sensor.caravan_temperature": "20"}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.caravan_temperature", "state": "20"}
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

    sched_module._last_snapshot = {"binary_sensor.garage_door": "off"}

    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "binary_sensor.garage_door", "state": "on"}
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
        {"entity_id": "sensor.caravan_temperature", "state": "20"}
    ])

    triage_fn = AsyncMock()
    scheduler = build_scheduler(mock_ha, triage_fn, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    triage_fn.assert_not_awaited()
    assert sched_module._last_snapshot == {"sensor.caravan_temperature": "20"}


def test_is_watched_allowlist():
    from jarvis.scheduler import _is_watched
    assert _is_watched("lock.front_door")
    assert _is_watched("binary_sensor.garage_door")
    assert _is_watched("sensor.caravan_temperature")
    assert not _is_watched("sensor.living_room_voltage")
    assert not _is_watched("input_text.attic_harvest_operator_status")


def test_allowlist_filters_states_before_diff():
    from jarvis.scheduler import compute_state_diff, _is_watched
    states = [
        {"entity_id": "binary_sensor.garage_door", "state": "on"},
        {"entity_id": "sensor.living_room_voltage", "state": "245.0"},
    ]
    watched = [s for s in states if _is_watched(s["entity_id"])]
    # The persistent snapshot only ever holds watched entities, so voltage was never tracked.
    last = {"binary_sensor.garage_door": "off"}
    _, diff = compute_state_diff(watched, last, domains=["binary_sensor", "sensor"])
    assert any("garage_door" in d for d in diff)
    assert not any("voltage" in d for d in diff)


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


def test_watch_list_overridable_via_env(monkeypatch):
    import importlib
    monkeypatch.setenv("PROACTIVE_WATCH", "spa_temp, pool_ph")
    monkeypatch.setenv("PROACTIVE_WATCH_DOMAINS", "lock,alarm_control_panel")
    import jarvis.scheduler as s
    importlib.reload(s)
    try:
        assert s._is_watched("sensor.spa_temp")
        assert s._is_watched("alarm_control_panel.house")
        assert s._is_watched("lock.front_door")
        assert not s._is_watched("binary_sensor.garage_door")  # default list replaced
    finally:
        monkeypatch.delenv("PROACTIVE_WATCH", raising=False)
        monkeypatch.delenv("PROACTIVE_WATCH_DOMAINS", raising=False)
        importlib.reload(s)  # restore defaults for other tests


def test_watch_list_defaults_when_unset(monkeypatch):
    import importlib
    monkeypatch.delenv("PROACTIVE_WATCH", raising=False)
    monkeypatch.delenv("PROACTIVE_WATCH_DOMAINS", raising=False)
    import jarvis.scheduler as s
    importlib.reload(s)
    assert s._is_watched("binary_sensor.garage_door")
    assert s._is_watched("lock.front_door")


async def test_insight_poll_includes_group_members(monkeypatch):
    import importlib
    monkeypatch.setenv("PROACTIVE_WATCH_GROUP", "group.jarvis_watch")
    from jarvis import scheduler as sched_module
    importlib.reload(sched_module)
    try:
        sched_module._last_snapshot = {"sensor.office_co2": "400"}
        mock_ha = MagicMock()
        mock_ha.get_states = AsyncMock(return_value=[
            {"entity_id": "sensor.office_co2", "state": "900"},  # not matched by substr/domain
        ])
        mock_ha.get_state = AsyncMock(
            return_value={"attributes": {"entity_id": ["sensor.office_co2"]}}
        )
        triage_fn = AsyncMock()
        scheduler = sched_module.build_scheduler(mock_ha, triage_fn, None, AsyncMock())
        jobs = {job.id: job for job in scheduler.get_jobs()}
        await jobs["insight_poll"].func()
        triage_fn.assert_awaited_once()
        assert "office_co2" in triage_fn.call_args[0][0]
    finally:
        monkeypatch.delenv("PROACTIVE_WATCH_GROUP", raising=False)
        importlib.reload(sched_module)


async def test_insight_poll_group_missing_is_graceful():
    from jarvis import scheduler as sched_module
    sched_module._last_snapshot = {"binary_sensor.garage_door": "off"}
    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "binary_sensor.garage_door", "state": "on"},
    ])
    mock_ha.get_state = AsyncMock(side_effect=Exception("no such group"))
    triage_fn = AsyncMock()
    scheduler = sched_module.build_scheduler(mock_ha, triage_fn, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()
    triage_fn.assert_awaited_once()  # garage_door still watched by substring
    assert "garage_door" in triage_fn.call_args[0][0]


async def test_morning_briefing_appends_caravan_question_and_records():
    from jarvis import scheduler as s
    from jarvis.config import config

    config.CARAVAN_PROMPT_ENABLED = True
    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[])
    mock_ha.get_state_summary = MagicMock(return_value="")
    sent = []
    recorded = []
    send_fn = AsyncMock(side_effect=lambda t: sent.append(t))
    recorder = AsyncMock(side_effect=lambda t: recorded.append(t))

    with patch("jarvis.agents.briefing.generate", new_callable=AsyncMock, return_value="Good morning."), \
         patch("jarvis.anomaly.detect_and_surface", new_callable=AsyncMock, return_value=[]):
        scheduler = s.build_scheduler(
            mock_ha, AsyncMock(), None, send_fn, briefing_recorder=recorder
        )
        jobs = {job.id: job for job in scheduler.get_jobs()}
        await jobs["morning_briefing"].func()

    assert len(sent) == 1
    assert "caravan" in sent[0].lower()
    # The exact same text is recorded into history for reply context.
    assert recorded == sent


async def test_morning_briefing_skips_caravan_when_disabled():
    from jarvis import scheduler as s
    from jarvis.config import config

    config.CARAVAN_PROMPT_ENABLED = False
    try:
        mock_ha = MagicMock()
        mock_ha.get_states = AsyncMock(return_value=[])
        mock_ha.get_state_summary = MagicMock(return_value="")
        sent = []
        send_fn = AsyncMock(side_effect=lambda t: sent.append(t))

        with patch("jarvis.agents.briefing.generate", new_callable=AsyncMock, return_value="Good morning."), \
             patch("jarvis.anomaly.detect_and_surface", new_callable=AsyncMock, return_value=[]):
            scheduler = s.build_scheduler(mock_ha, AsyncMock(), None, send_fn)
            jobs = {job.id: job for job in scheduler.get_jobs()}
            await jobs["morning_briefing"].func()

        assert sent == ["Good morning."]
    finally:
        config.CARAVAN_PROMPT_ENABLED = True


async def test_morning_briefing_arms_safety_net():
    """When the caravan question is asked, a decision is marked pending for today."""
    from jarvis import scheduler as s
    from jarvis import caravan
    from jarvis.config import config

    config.CARAVAN_PROMPT_ENABLED = True
    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[])
    mock_ha.get_state_summary = MagicMock(return_value="")

    with patch("jarvis.agents.briefing.generate", new_callable=AsyncMock, return_value="Morning."), \
         patch("jarvis.anomaly.detect_and_surface", new_callable=AsyncMock, return_value=[]):
        scheduler = s.build_scheduler(mock_ha, AsyncMock(), None, AsyncMock())
        jobs = {job.id: job for job in scheduler.get_jobs()}
        await jobs["morning_briefing"].func()

    assert caravan.decision_pending() is True


async def test_caravan_safety_net_forces_off_when_unanswered():
    """No decision by safety hour -> caravan entities forced off; notify only if it was on."""
    from jarvis import scheduler as s
    from jarvis import caravan
    from jarvis.config import config

    config.CARAVAN_PROMPT_ENABLED = True
    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    caravan.mark_prompt_sent()  # question asked, no decision made

    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "on"})
    mock_ha.call_service = AsyncMock(return_value=[])
    send_fn = AsyncMock()

    scheduler = s.build_scheduler(mock_ha, AsyncMock(), None, send_fn)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["caravan_safety_net"].func()

    mock_ha.call_service.assert_awaited_once_with(
        "input_boolean", "turn_off", {"entity_id": "input_boolean.caravan_heater_enabled"}
    )
    send_fn.assert_awaited_once()  # it was on, so we tell the user
    assert caravan.decision_pending() is False  # enforcement marks it decided


async def test_caravan_safety_net_silent_when_already_off():
    from jarvis import scheduler as s
    from jarvis import caravan
    from jarvis.config import config

    config.CARAVAN_PROMPT_ENABLED = True
    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    caravan.mark_prompt_sent()

    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "off"})
    mock_ha.call_service = AsyncMock(return_value=[])
    send_fn = AsyncMock()

    scheduler = s.build_scheduler(mock_ha, AsyncMock(), None, send_fn)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["caravan_safety_net"].func()

    mock_ha.call_service.assert_awaited_once()  # still forces off (idempotent)
    send_fn.assert_not_awaited()  # but stays silent


async def test_caravan_safety_net_skips_when_decided():
    """If the user already answered, the safety net does nothing."""
    from jarvis import scheduler as s
    from jarvis import caravan
    from jarvis.config import config

    config.CARAVAN_PROMPT_ENABLED = True
    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    caravan.mark_prompt_sent()
    caravan.mark_decided()  # user replied

    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "on"})
    mock_ha.call_service = AsyncMock(return_value=[])
    send_fn = AsyncMock()

    scheduler = s.build_scheduler(mock_ha, AsyncMock(), None, send_fn)
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["caravan_safety_net"].func()

    mock_ha.call_service.assert_not_awaited()
    send_fn.assert_not_awaited()


async def test_resolve_mode_reads_input_select(monkeypatch):
    from jarvis import scheduler as s
    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "away"})
    assert await s.resolve_mode(mock_ha) == "away"


async def test_resolve_mode_unknown_falls_back(monkeypatch):
    from jarvis import scheduler as s
    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "banana"})
    assert await s.resolve_mode(mock_ha) == "standard"


async def test_resolve_mode_missing_entity_falls_back():
    from jarvis import scheduler as s
    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(side_effect=Exception("no such entity"))
    assert await s.resolve_mode(mock_ha) == "standard"


def test_is_watched_in_mode_extra_watch():
    from jarvis.scheduler import _is_watched_in_mode
    # motion is watched in away, not in standard
    assert _is_watched_in_mode("binary_sensor.hall_motion", "away")
    assert not _is_watched_in_mode("binary_sensor.hall_motion", "standard")
    # weather watched in storm
    assert _is_watched_in_mode("weather.home", "storm")
    # base allow-list still applies in any mode
    assert _is_watched_in_mode("binary_sensor.garage_door", "quiet")


def test_mode_poll_min_values():
    from jarvis.scheduler import _mode_poll_min
    from jarvis.config import config
    assert _mode_poll_min("quiet") == 30
    assert _mode_poll_min("away") == 5
    assert _mode_poll_min("storm") == 5
    assert _mode_poll_min("standard") == int(config.POLL_INTERVAL_MIN)
