import pytest
from unittest.mock import AsyncMock, MagicMock, patch

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [
        ("TELEGRAM_BOT_TOKEN", "t"),
        ("TELEGRAM_CHAT_ID", "1"),
        ("HA_TOKEN", "h"),
        ("ANTHROPIC_API_KEY", "sk"),
    ]:
        monkeypatch.setenv(k, v)


def _make_mock_ha(
    calendar_entities=None,
    todo_entities=None,
    weather_entities=None,
    call_service_return=None,
):
    """Build a mock HAClient with configurable entity lists."""
    mock = MagicMock()

    async def get_entities_by_domain(domain):
        if domain == "calendar":
            return calendar_entities or []
        if domain == "todo":
            return todo_entities or []
        if domain == "weather":
            return weather_entities or []
        return []

    mock.get_entities_by_domain = AsyncMock(side_effect=get_entities_by_domain)
    mock.call_service = AsyncMock(return_value=call_service_return or {})
    return mock


async def test_generate_returns_string():
    from jarvis.agents.sunday_briefing import generate

    mock_ha = _make_mock_ha()

    with patch(
        "jarvis.router.complete",
        new_callable=AsyncMock,
        return_value="Your week looks manageable.",
    ):
        result = await generate(mock_ha)

    assert isinstance(result, str)
    assert len(result) > 10


async def test_generate_calls_router_with_sunday_briefing_role():
    from jarvis.agents.sunday_briefing import generate

    mock_ha = _make_mock_ha()
    captured_args = []

    async def capture_complete(role, messages, **kwargs):
        captured_args.append({"role": role, "messages": messages, **kwargs})
        return "Weekly plan here."

    with patch("jarvis.router.complete", new_callable=AsyncMock, side_effect=capture_complete):
        await generate(mock_ha)

    assert len(captured_args) == 1
    assert captured_args[0]["role"] == "sunday_briefing"
    assert captured_args[0]["max_tokens"] == 1200


async def test_gather_context_includes_all_sections():
    from jarvis.agents.sunday_briefing import gather_context

    mock_ha = _make_mock_ha(
        calendar_entities=[
            {"entity_id": "calendar.personal", "state": "off", "attributes": {"friendly_name": "Personal"}},
        ],
        todo_entities=[
            {"entity_id": "todo.shopping", "state": "1", "attributes": {"friendly_name": "Shopping"}},
        ],
        weather_entities=[
            {
                "entity_id": "weather.home",
                "state": "sunny",
                "attributes": {"friendly_name": "Home", "temperature": 22, "temperature_unit": "°C"},
            },
        ],
        call_service_return={},
    )

    context = await gather_context(mock_ha)

    assert "CALENDAR" in context
    assert "TODO" in context
    assert "WEATHER" in context
    assert "GOALS" in context
    assert "Sunday briefing request" in context


async def test_gather_context_handles_no_entities():
    from jarvis.agents.sunday_briefing import gather_context

    mock_ha = _make_mock_ha()
    context = await gather_context(mock_ha)

    assert "No calendar entities" in context
    assert "No todo list entities" in context
    assert "No weather entities" in context


async def test_extract_events_handles_dict_format():
    from jarvis.agents.sunday_briefing import _extract_events

    # HA returns {entity_id: {"events": [...]}}
    response = {
        "calendar.personal": {
            "events": [
                {"summary": "Dentist", "start": "2026-03-26T10:00", "end": "2026-03-26T11:00"},
            ]
        }
    }
    events = _extract_events(response, "calendar.personal")
    assert len(events) == 1
    assert events[0]["summary"] == "Dentist"


async def test_extract_events_handles_list_format():
    from jarvis.agents.sunday_briefing import _extract_events

    # Sometimes HA returns a list of state dicts
    response = [
        {
            "entity_id": "calendar.work",
            "attributes": {
                "events": [
                    {"summary": "Standup", "start": "2026-03-26T09:00", "end": "2026-03-26T09:30"},
                ]
            },
        }
    ]
    events = _extract_events(response, "calendar.work")
    assert len(events) == 1
    assert events[0]["summary"] == "Standup"


async def test_extract_todo_items_handles_dict_format():
    from jarvis.agents.sunday_briefing import _extract_todo_items

    response = {
        "todo.shopping": {
            "items": [
                {"summary": "Milk", "status": "needs_action"},
                {"summary": "Bread", "status": "completed"},
            ]
        }
    }
    items = _extract_todo_items(response, "todo.shopping")
    assert len(items) == 2


async def test_generate_handles_llm_failure():
    from jarvis.agents.sunday_briefing import generate

    mock_ha = _make_mock_ha()

    with patch(
        "jarvis.router.complete",
        new_callable=AsyncMock,
        side_effect=RuntimeError("API down"),
    ):
        result = await generate(mock_ha)

    assert "unavailable" in result.lower()


async def test_gather_calendar_events_handles_service_error():
    from jarvis.agents.sunday_briefing import _gather_calendar_events

    mock_ha = _make_mock_ha(
        calendar_entities=[
            {"entity_id": "calendar.personal", "state": "off", "attributes": {"friendly_name": "Personal"}},
        ],
    )
    mock_ha.call_service = AsyncMock(side_effect=Exception("service not found"))

    result = await _gather_calendar_events(mock_ha)
    assert "unavailable" in result.lower()


async def test_gather_weather_uses_attribute_forecast_as_fallback():
    from jarvis.agents.sunday_briefing import _gather_weather_forecast

    mock_ha = _make_mock_ha(
        weather_entities=[
            {
                "entity_id": "weather.home",
                "state": "cloudy",
                "attributes": {
                    "friendly_name": "Home",
                    "temperature": 15,
                    "temperature_unit": "°C",
                    "forecast": [
                        {"datetime": "2026-03-26T00:00:00", "condition": "rainy"},
                        {"datetime": "2026-03-27T00:00:00", "condition": "sunny"},
                    ],
                },
            },
        ],
    )
    mock_ha.call_service = AsyncMock(side_effect=Exception("service not available"))

    result = await _gather_weather_forecast(mock_ha)
    assert "rainy" in result
    assert "sunny" in result


def test_scheduler_has_sunday_briefing_job():
    from jarvis.scheduler import build_scheduler

    mock_ha = MagicMock()
    scheduler = build_scheduler(mock_ha, AsyncMock(), None, AsyncMock())
    job_ids = [job.id for job in scheduler.get_jobs()]
    assert "sunday_briefing" in job_ids


async def test_sunday_briefing_job_calls_generate():
    from jarvis.scheduler import build_scheduler

    mock_ha = MagicMock()
    send_fn = AsyncMock()
    scheduler = build_scheduler(mock_ha, AsyncMock(), None, send_fn)
    jobs = {job.id: job for job in scheduler.get_jobs()}

    with patch(
        "jarvis.agents.sunday_briefing.generate",
        new_callable=AsyncMock,
        return_value="Your week ahead.",
    ):
        await jobs["sunday_briefing"].func()

    send_fn.assert_awaited_once_with("Your week ahead.")
