import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import json
from datetime import datetime, timedelta, timezone

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
    s._last_recommendations = {}
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
def test_extract_recommendation_signals_mixed_snapshot():
    from jarvis.scheduler import extract_recommendation_signals

    states = [
        {"entity_id": "sensor.energy_tariff", "state": "peak", "attributes": {}},
        {"entity_id": "sensor.solar_power", "state": "2400", "attributes": {"unit_of_measurement": "W"}},
        {"entity_id": "weather.home", "state": "rainy", "attributes": {}},
        {"entity_id": "person.brook", "state": "not_home", "attributes": {}},
        {"entity_id": "switch.ev_charger", "state": "off", "attributes": {"friendly_name": "EV Charger"}},
    ]

    signals = extract_recommendation_signals(states, ["sensor.energy_tariff: low -> peak"])
    assert len(signals.tariff_entities) == 1
    assert len(signals.solar_entities) == 1
    assert len(signals.weather_entities) == 1
    assert len(signals.away_entities) == 1
    assert len(signals.available_shiftable_loads) == 1


def test_classify_shiftable_load_accepts_expected_devices():
    from jarvis.scheduler import classify_shiftable_load

    assert classify_shiftable_load({
        "entity_id": "climate.office_heat_pump",
        "state": "cooling",
        "attributes": {"friendly_name": "Office Heat Pump"},
    }) is not None
    assert classify_shiftable_load({
        "entity_id": "switch.ev_charger",
        "state": "off",
        "attributes": {"friendly_name": "EV Charger"},
    }) is not None
    assert classify_shiftable_load({
        "entity_id": "fan.bathroom_extractor",
        "state": "on",
        "attributes": {"friendly_name": "Bathroom Extractor"},
    }) is not None


def test_classify_shiftable_load_rejects_protected_devices():
    from jarvis.scheduler import classify_shiftable_load

    assert classify_shiftable_load({
        "entity_id": "switch.kitchen_fridge",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Fridge"},
    }) is None
    assert classify_shiftable_load({
        "entity_id": "camera.front_door",
        "state": "streaming",
        "attributes": {},
    }) is None
    assert classify_shiftable_load({
        "entity_id": "lock.front_door",
        "state": "locked",
        "attributes": {},
    }) is None


def test_energy_candidates_cover_delay_solar_and_away():
    from jarvis.scheduler import extract_recommendation_signals, generate_energy_candidates

    states = [
        {"entity_id": "sensor.energy_tariff", "state": "peak", "attributes": {}},
        {"entity_id": "sensor.solar_power", "state": "3200", "attributes": {"unit_of_measurement": "W"}},
        {"entity_id": "person.brook", "state": "not_home", "attributes": {}},
        {"entity_id": "climate.lounge_heat_pump", "state": "heating", "attributes": {"friendly_name": "Lounge Heat Pump"}},
        {"entity_id": "switch.ev_charger", "state": "off", "attributes": {"friendly_name": "EV Charger"}},
    ]
    signals = extract_recommendation_signals(states, ["climate.lounge_heat_pump: idle -> heating"])
    candidates = generate_energy_candidates(signals)
    kinds = {candidate.recommendation_type for candidate in candidates}
    assert "energy.delay_load" in kinds
    assert "energy.use_solar_window" in kinds
    assert "energy.stop_away_load" in kinds


def test_weather_candidate_generated_for_rain_and_open_window():
    from jarvis.scheduler import extract_recommendation_signals, generate_weather_candidates

    states = [
        {"entity_id": "weather.home", "state": "rainy", "attributes": {}},
        {"entity_id": "cover.kitchen_window", "state": "open", "attributes": {}},
    ]
    signals = extract_recommendation_signals(states, ["cover.kitchen_window: closed -> open"])
    candidates = generate_weather_candidates(signals)
    assert any(candidate.recommendation_type == "weather.close_exposed_opening" for candidate in candidates)


def test_presence_candidate_generated_when_everyone_away():
    from jarvis.scheduler import extract_recommendation_signals, generate_presence_candidates

    states = [
        {"entity_id": "person.brook", "state": "not_home", "attributes": {}},
        {"entity_id": "switch.spa_pool", "state": "on", "attributes": {"friendly_name": "Spa Pool"}},
    ]
    signals = extract_recommendation_signals(states, ["switch.spa_pool: off -> on"])
    candidates = generate_presence_candidates(signals)
    assert any(candidate.recommendation_type == "presence.stop_when_away" for candidate in candidates)


def test_feedback_adjustments_boost_and_decay(tmp_path):
    from jarvis.scheduler import apply_feedback_adjustments, load_feedback_store, RecommendationCandidate

    feedback_path = tmp_path / "recommendation_feedback.json"
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    feedback_path.write_text(json.dumps({
        "devices": {
            "climate.office_heat_pump": {
                "accepted": 1, "ignored": 0, "dismissed": 0, "corrected": 0, "last_feedback_at": old, "notes": []
            }
        },
        "recommendation_types": {
            "energy.delay_load": {
                "accepted": 1, "ignored": 0, "dismissed": 0, "corrected": 0, "last_feedback_at": old
            }
        },
    }))
    store = load_feedback_store(feedback_path)
    candidate = RecommendationCandidate(
        category="energy",
        recommendation_type="energy.delay_load",
        score=80,
        confidence=0.70,
        impact=80,
        urgency=70,
        annoyance_risk=20,
        action="Delay it.",
        reason="Because.",
        entities=("climate.office_heat_pump",),
        evidence=(),
        dedupe_key="energy:climate.office_heat_pump:delay_load",
    )
    adjusted = apply_feedback_adjustments([candidate], store)[0]
    assert adjusted.score > 80
    assert adjusted.confidence > 0.70
    assert adjusted.score < 90


def test_rank_recommendation_candidates_orders_by_score():
    from jarvis.scheduler import RecommendationCandidate, rank_recommendation_candidates

    low = RecommendationCandidate("weather", "weather.prep_for_rain", 82, 0.72, 70, 70, 20, "a", "b", (), (), "w:1")
    high = RecommendationCandidate("energy", "energy.delay_load", 91, 0.75, 80, 80, 20, "a", "b", (), (), "e:1")
    ranked = rank_recommendation_candidates([low, high])
    assert ranked[0].dedupe_key == "e:1"


def test_select_recommendation_suppresses_below_threshold_and_dedupe():
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import RecommendationCandidate, select_recommendation

    sched_module._last_recommendations = {
        "energy:climate.office_heat_pump:delay_load": (datetime.now(timezone.utc), 90)
    }
    candidate = RecommendationCandidate(
        category="energy",
        recommendation_type="energy.delay_load",
        score=92,
        confidence=0.80,
        impact=80,
        urgency=80,
        annoyance_risk=20,
        action="Delay it.",
        reason="Because.",
        entities=("climate.office_heat_pump",),
        evidence=(),
        dedupe_key="energy:climate.office_heat_pump:delay_load",
    )
    decision = select_recommendation([candidate], [])
    assert decision.candidate is None
    assert "duplicate" in (decision.suppressed_reason or "")


def test_build_recommendation_context_contains_selected_recommendation():
    from jarvis.scheduler import (
        RecommendationCandidate,
        RecommendationDecision,
        RecommendationSignal,
        build_recommendation_context,
    )

    candidate = RecommendationCandidate(
        category="energy",
        recommendation_type="energy.delay_load",
        score=94,
        confidence=0.82,
        impact=88,
        urgency=72,
        annoyance_risk=20,
        action="Delay or turn off climate.lounge_heat_pump until cheaper power.",
        reason="Power pricing is high.",
        entities=("climate.lounge_heat_pump",),
        evidence=("sensor.energy_tariff: peak", "climate.lounge_heat_pump: heating"),
        dedupe_key="energy:climate.lounge_heat_pump:delay_load",
    )
    signals = RecommendationSignal(
        diff_lines=("climate.lounge_heat_pump: idle -> heating",),
        signal_lines=(),
        tariff_entities=(),
        solar_entities=(),
        weather_entities=(),
        away_entities=(),
        home_entities=(),
        open_entities=(),
        shiftable_loads=(),
        active_shiftable_loads=(),
        available_shiftable_loads=(),
    )
    text = build_recommendation_context(
        RecommendationDecision(candidate, None, ("weather: score 73, below send threshold",)),
        signals,
        recent_alerts=["Previous recommendation"],
        top_candidates=[candidate],
    )
    assert "Selected recommendation:" in text
    assert "Suppressed alternatives:" in text
    assert "Recent alerts already sent:" in text


async def test_insight_poll_only_calls_callback_for_selected_recommendation():
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import build_scheduler

    sched_module._last_snapshot = {"sensor.energy_tariff": "low", "climate.lounge_heat_pump": "idle"}
    sched_module._last_recommendations = {}
    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.energy_tariff", "state": "peak", "attributes": {}},
        {"entity_id": "climate.lounge_heat_pump", "state": "heating", "attributes": {"friendly_name": "Lounge Heat Pump"}},
    ])

    callback = AsyncMock()
    scheduler = build_scheduler(mock_ha, callback, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    callback.assert_awaited_once()
    context, metadata = callback.call_args[0]
    assert "Selected recommendation:" in context
    assert metadata["recommendation_type"] == "energy.delay_load"


async def test_insight_poll_stays_silent_for_weak_candidates():
    from jarvis import scheduler as sched_module
    from jarvis.scheduler import build_scheduler

    sched_module._last_snapshot = {"weather.home": "cloudy", "cover.kitchen_window": "closed"}
    sched_module._last_recommendations = {}
    mock_ha = MagicMock()
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "weather.home", "state": "sunny", "attributes": {}},
        {"entity_id": "cover.kitchen_window", "state": "open", "attributes": {}},
    ])

    callback = AsyncMock()
    scheduler = build_scheduler(mock_ha, callback, None, AsyncMock())
    jobs = {job.id: job for job in scheduler.get_jobs()}
    await jobs["insight_poll"].func()

    callback.assert_not_awaited()
