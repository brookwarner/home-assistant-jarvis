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


async def test_send_message_tool_calls_send_fn():
    """send_message tool calls the injected send_fn."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    mock_ha = MagicMock(spec=HAClient)
    send_fn = AsyncMock()
    agent = ConversationAgent(mock_ha, send_fn=send_fn)

    result = await agent._execute_tool("send_message", {"text": "Hello Brook"})

    send_fn.assert_awaited_once_with("Hello Brook")
    assert result == {"status": "sent"}


async def test_send_message_no_send_fn():
    """send_message without a send_fn returns error."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    agent = ConversationAgent(MagicMock(spec=HAClient))
    result = await agent._execute_tool("send_message", {"text": "Hello"})

    assert "error" in result


async def test_ask_user_sends_prompt_and_returns_reply():
    """ask_user sends the prompt and returns the reply that resolves the future."""
    import asyncio
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(spec=HAClient), send_fn=send_fn)

    # Simulate user replying after a short delay
    async def reply_after_delay():
        await asyncio.sleep(0.05)
        # At this point _pending_reply should be set — resolve it
        assert agent._pending_reply is not None
        agent._pending_reply.set_result("yes please")

    asyncio.create_task(reply_after_delay())
    result = await agent._execute_tool("ask_user", {"prompt": "Turn off the spa?", "timeout_seconds": 2})

    send_fn.assert_awaited_once_with("Turn off the spa?")
    assert result == {"reply": "yes please"}
    assert agent._pending_reply is None  # cleaned up


async def test_ask_user_timeout():
    """ask_user returns timeout message if no reply arrives."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(spec=HAClient), send_fn=send_fn)

    result = await agent._execute_tool("ask_user", {"prompt": "Hello?", "timeout_seconds": 0})

    assert "timed out" in result["reply"]
    assert agent._pending_reply is None

async def test_run_proactive_sends_response():
    """run_proactive runs the tool loop and sends the final text via send_fn."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(spec=HAClient), send_fn=send_fn)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "Spa has been on 4 hours."
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.run_proactive("spa still running", chat_id=123)

    send_fn.assert_awaited_once_with("Spa has been on 4 hours.")
    # HA event (use_history=True default) should persist to history
    assert len(agent._history[123]) == 2  # [PROACTIVE] user msg + assistant response


async def test_run_proactive_no_history_for_polls():
    """run_proactive with use_history=False does not pollute conversation history."""
    from jarvis.agents.conversation import ConversationAgent

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(), send_fn=send_fn)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "SILENT"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.run_proactive("heartbeat check", chat_id=123, use_history=False)

    # History must stay empty — periodic polls must not pollute conversation
    assert len(agent._history[123]) == 0


async def test_run_proactive_suppresses_silent():
    """run_proactive does not send if agent returns SILENT."""
    from jarvis.agents.conversation import ConversationAgent

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(), send_fn=send_fn)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "SILENT"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.run_proactive("routine check", chat_id=123)

    send_fn.assert_not_awaited()


async def test_agent_busy_flag_set_during_reply():
    """_agent_busy is True while reply is running, False after."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock())
    assert agent._agent_busy is False

    busy_during = []

    async def fake_completion(**kwargs):
        busy_during.append(agent._agent_busy)
        m = MagicMock()
        m.choices[0].finish_reason = "stop"
        m.choices[0].message.content = "done"
        m.choices[0].message.tool_calls = None
        return m

    with patch("litellm.acompletion", side_effect=fake_completion):
        await agent.reply(chat_id=1, user_text="hi")

    assert busy_during == [True]
    assert agent._agent_busy is False
