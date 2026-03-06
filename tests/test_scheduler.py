import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mock_env(monkeypatch, tmp_path):
    for key, value in [
        ("TELEGRAM_BOT_TOKEN", "t"),
        ("TELEGRAM_CHAT_ID", "1"),
        ("HA_TOKEN", "h"),
        ("ANTHROPIC_API_KEY", "sk"),
    ]:
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)


async def test_check_user_alerts_fires_when_above_threshold(tmp_path):
    from jarvis.scheduler import check_user_alerts

    alerts_file = tmp_path / "user_alerts.json"
    alerts_file.write_text(json.dumps([{
        "id": "1",
        "entity_id": "sensor.attic_temp",
        "condition": "above",
        "threshold": 35.0,
        "message": "Attic hot!",
        "enabled": True,
    }]))

    mock_ha = MagicMock()
    mock_ha.get_state = AsyncMock(return_value={"state": "38.0", "attributes": {}})
    triggered = []

    async def on_trigger(message):
        triggered.append(message)

    await check_user_alerts(mock_ha, on_trigger, alerts_path=str(alerts_file))
    assert len(triggered) == 1
    assert "Attic hot!" in triggered[0]


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
