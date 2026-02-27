import pytest
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

async def test_classify_returns_valid_action():
    from jarvis.agents.triage import classify

    with patch("jarvis.router.complete", new_callable=AsyncMock, return_value="notify"):
        result = await classify(
            event={"title": "Security", "message": "Garage door opened", "entity_id": ""},
            ha_context="switch.spa: on\nsensor.lounge_temp: 18°C",
        )

    assert result in ("ignore", "log", "notify", "needs_input")

async def test_classify_defaults_to_notify_on_error():
    from jarvis.agents.triage import classify

    with patch("jarvis.router.complete", new_callable=AsyncMock, side_effect=Exception("API error")):
        result = await classify(
            event={"title": "Security", "message": "Door open", "entity_id": ""},
            ha_context="",
        )

    assert result == "notify"  # Fail safe

async def test_classify_strips_whitespace_and_lowercases():
    from jarvis.agents.triage import classify

    with patch("jarvis.router.complete", new_callable=AsyncMock, return_value="  IGNORE\n"):
        result = await classify(
            event={"title": "Test", "message": "Minor event", "entity_id": ""},
            ha_context="",
        )

    assert result == "ignore"
