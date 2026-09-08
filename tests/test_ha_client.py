import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
import aiohttp

# We test with a real-ish async HTTP mock
pytestmark = pytest.mark.asyncio

@pytest.fixture
def mock_config(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "ha_token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

async def test_get_state_returns_state_dict(mock_config):
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={
        "entity_id": "sensor.attic_temperature",
        "state": "28.5",
        "attributes": {"unit_of_measurement": "°C"},
    })
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_response):
        result = await client.get_state("sensor.attic_temperature")

    assert result["state"] == "28.5"
    assert result["entity_id"] == "sensor.attic_temperature"

async def test_call_service_posts_correctly(mock_config):
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[])
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.post", return_value=mock_response) as mock_post:
        await client.call_service("switch", "turn_on", {"entity_id": "switch.spa"})

    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert "switch/turn_on" in call_kwargs[0][0]

async def test_get_states_returns_list(mock_config):
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[
        {"entity_id": "sensor.temp", "state": "20"},
        {"entity_id": "switch.spa", "state": "on"},
    ])
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_response):
        result = await client.get_states()

    assert len(result) == 2
    assert result[0]["entity_id"] == "sensor.temp"

async def test_get_state_summary_excludes_entities(mock_config):
    """Entities in `exclude` are dropped from the summary so stale/dubious sensors
    (e.g. the hardcoded 'Water Usage vs NZ Average' template) never reach the briefing."""
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")

    states = [
        {"entity_id": "sensor.lounge_temp", "state": "19", "attributes": {"unit_of_measurement": "°C"}},
        {"entity_id": "sensor.water_usage_vs_average", "state": "174.0", "attributes": {"unit_of_measurement": "%"}},
    ]

    summary = client.get_state_summary(states, domains=["sensor"], exclude={"sensor.water_usage_vs_average"})

    assert "sensor.lounge_temp" in summary
    assert "water_usage_vs_average" not in summary
    assert "174.0" not in summary


async def test_get_state_summary_no_exclude_keeps_everything(mock_config):
    """Default (no exclude) is unchanged — backward compatible."""
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")
    states = [{"entity_id": "sensor.water_usage_vs_average", "state": "174.0", "attributes": {}}]
    summary = client.get_state_summary(states, domains=["sensor"])
    assert "water_usage_vs_average" in summary


async def test_get_calendar_events_requests_the_given_window(mock_config):
    """Calendar events come from /api/calendars/<entity>?start=&end=, which is a different
    endpoint from /api/states — a calendar entity's own state is only on/off and carries no
    event detail."""
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value=[
        {"start": {"date": "2026-07-23"}, "end": {"date": "2026-07-24"},
         "summary": "A birthday"},
    ])
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_response) as mock_get:
        events = await client.get_calendar_events(
            "calendar.birthdays",
            "2026-07-26T00:00:00.000Z",
            "2026-07-27T00:00:00.000Z",
        )

    assert events[0]["summary"] == "A birthday"
    url = mock_get.call_args[0][0]
    params = mock_get.call_args[1]["params"]
    assert "/api/calendars/calendar.birthdays" in url
    assert params["start"] == "2026-07-26T00:00:00.000Z"
    assert params["end"] == "2026-07-27T00:00:00.000Z"


async def test_get_timezone_reads_ha_config(mock_config):
    from jarvis.ha_client import HAClient
    client = HAClient("http://localhost:8123", "ha_token")

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.json = AsyncMock(return_value={"time_zone": "Pacific/Auckland", "version": "x"})
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_response):
        tz = await client.get_timezone()

    assert tz == "Pacific/Auckland"
