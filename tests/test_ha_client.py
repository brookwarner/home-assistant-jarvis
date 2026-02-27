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
