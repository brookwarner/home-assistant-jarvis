import pytest
from unittest.mock import AsyncMock, patch

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN","t"),("TELEGRAM_CHAT_ID","1"),
                 ("HA_TOKEN","h"),("ANTHROPIC_API_KEY","sk")]:
        monkeypatch.setenv(k, v)

async def test_generate_returns_string():
    from jarvis.agents.briefing import generate

    with patch("jarvis.router.complete", new_callable=AsyncMock, return_value="Good morning! Energy: 2.3kWh overnight."):
        result = await generate(ha_state_summary="sensor.spa: on\nsensor.temp: 18°C")

    assert isinstance(result, str)
    assert len(result) > 10

async def test_generate_includes_time_context():
    from jarvis.agents.briefing import generate
    import jarvis.agents.briefing as b_module

    captured_messages = []
    async def capture(*args, **kwargs):
        captured_messages.extend(kwargs.get("messages", args[1] if len(args) > 1 else []))
        return "Morning summary."

    with patch("jarvis.router.complete", new_callable=AsyncMock, side_effect=capture):
        await generate(ha_state_summary="sensor.temp: 18°C")

    full_text = " ".join(m["content"] for m in captured_messages)
    assert "morning" in full_text.lower() or "briefing" in full_text.lower()
