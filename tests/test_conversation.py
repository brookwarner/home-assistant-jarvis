import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from collections import defaultdict

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN","t"),("TELEGRAM_CHAT_ID","1"),
                 ("HA_TOKEN","h"),("ANTHROPIC_API_KEY","sk-ant-test")]:
        monkeypatch.setenv(k, v)

async def test_reply_returns_text_response():
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    mock_ha = MagicMock(spec=HAClient)
    mock_ha.get_states = AsyncMock(return_value=[
        {"entity_id": "sensor.lounge_temp", "state": "19", "attributes": {"unit_of_measurement": "°C"}}
    ])
    mock_ha.get_state_summary = MagicMock(return_value="sensor.lounge_temp: 19°C")

    agent = ConversationAgent(mock_ha)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "The lounge is 19°C."
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        result = await agent.reply(chat_id=123, user_text="What's the lounge temp?")

    assert isinstance(result, str)
    assert len(result) > 0

async def test_reply_maintains_history():
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    mock_ha = MagicMock(spec=HAClient)
    mock_ha.get_states = AsyncMock(return_value=[])
    mock_ha.get_state_summary = MagicMock(return_value="")

    agent = ConversationAgent(mock_ha)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "Sure."
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.reply(chat_id=999, user_text="First message")
        await agent.reply(chat_id=999, user_text="Second message")

    history = agent._history[999]
    assert len(history) >= 2


def test_agent_accepts_send_fn():
    """Agent stores send_fn and starts with no pending reply."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    mock_ha = MagicMock(spec=HAClient)
    send_fn = AsyncMock()

    agent = ConversationAgent(mock_ha, send_fn=send_fn)

    assert agent._send_fn is send_fn
    assert agent._pending_reply is None
    assert agent._agent_busy is False
