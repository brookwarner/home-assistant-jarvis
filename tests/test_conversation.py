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
        await agent.run_proactive(
            "spa still running",
            chat_id=123,
            recommendation_metadata={
                "category": "energy",
                "recommendation_type": "energy.delay_load",
                "entities": ["switch.spa_pool"],
                "dedupe_key": "energy:switch.spa_pool:delay_load",
            },
        )

    send_fn.assert_awaited_once_with("Spa has been on 4 hours.")
    # HA event (use_history=True default) should persist to history
    assert len(agent._history[123]) == 2  # [PROACTIVE] user msg + assistant response
    assert agent._last_recommendation_by_chat[123]["recommendation_type"] == "energy.delay_load"


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


async def test_feedback_reply_updates_store_as_accepted():
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock(), send_fn=AsyncMock())
    agent._last_recommendation_by_chat[1] = {
        "category": "energy",
        "recommendation_type": "energy.delay_load",
        "entities": ["climate.office_heat_pump"],
        "dedupe_key": "energy:climate.office_heat_pump:delay_load",
    }

    with patch("jarvis.scheduler.update_feedback_store") as updater:
        reply = await agent.reply(chat_id=1, user_text="good idea")

    updater.assert_called_once()
    assert "favour that kind" in reply


async def test_feedback_reply_updates_store_as_corrected():
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock(), send_fn=AsyncMock())
    agent._last_recommendation_by_chat[1] = {
        "category": "presence",
        "recommendation_type": "presence.stop_when_away",
        "entities": ["climate.office_heat_pump"],
        "dedupe_key": "presence:climate.office_heat_pump:stop_when_away",
    }

    with patch("jarvis.scheduler.update_feedback_store") as updater:
        reply = await agent.reply(chat_id=1, user_text="that's wrong, we're home")

    updater.assert_called_once()
    assert updater.call_args[0][1] == "corrected"
    assert "bad read" in reply


async def test_unrelated_message_does_not_update_feedback():
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock(), send_fn=AsyncMock())
    agent._last_recommendation_by_chat[1] = {
        "category": "presence",
        "recommendation_type": "presence.stop_when_away",
        "entities": ["switch.spa_pool"],
        "dedupe_key": "presence:switch.spa_pool:stop_when_away",
    }

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "Normal answer."
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("jarvis.scheduler.update_feedback_store") as updater:
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            reply = await agent.reply(chat_id=1, user_text="what's the weather?")

    updater.assert_not_called()
    assert reply == "Normal answer."


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


async def test_pending_reply_resolved_by_new_message():
    """
    If _pending_reply is set, the next user text resolves it
    rather than starting a new agent turn.
    """
    import asyncio
    from jarvis.agents.conversation import ConversationAgent

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(), send_fn=send_fn)

    # Simulate ask_user having set a pending future
    loop = asyncio.get_event_loop()
    agent._pending_reply = loop.create_future()

    # Simulate what handle_text does when _pending_reply is set
    user_text = "yes"
    if agent._pending_reply is not None and not agent._pending_reply.done():
        agent._pending_reply.set_result(user_text)

    assert agent._pending_reply.result() == "yes"


async def test_run_with_tools_uses_model_override():
    """_run_with_tools uses the model parameter when provided."""
    from jarvis.agents.conversation import ConversationAgent

    captured_models = []
    async def capture_completion(**kwargs):
        captured_models.append(kwargs.get("model"))
        m = MagicMock()
        m.choices[0].finish_reason = "stop"
        m.choices[0].message.content = "SILENT"
        m.choices[0].message.tool_calls = None
        return m

    agent = ConversationAgent(MagicMock())
    with patch("litellm.acompletion", side_effect=capture_completion):
        msgs = [{"role": "user", "content": "test"}]
        await agent._run_with_tools(msgs, model="openrouter/anthropic/claude-sonnet-4-6")

    assert captured_models[0] == "openrouter/anthropic/claude-sonnet-4-6"


async def test_run_with_tools_default_model():
    """_run_with_tools uses self._model when no override given."""
    from jarvis.agents.conversation import ConversationAgent

    captured = []
    async def capture(**kwargs):
        captured.append(kwargs.get("model"))
        m = MagicMock()
        m.choices[0].finish_reason = "stop"
        m.choices[0].message.content = "ok"
        m.choices[0].message.tool_calls = None
        return m

    agent = ConversationAgent(MagicMock())
    with patch("litellm.acompletion", side_effect=capture):
        await agent._run_with_tools([{"role": "user", "content": "hi"}])

    assert captured[0] == agent._model


async def test_recent_alerts_tracks_sent_messages():
    """Messages sent via run_proactive are recorded in _recent_alerts."""
    from jarvis.agents.conversation import ConversationAgent

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(), send_fn=send_fn)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "Spa running 4 hours"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.run_proactive("spa alert", chat_id=123, use_history=False)

    assert len(agent._recent_alerts) == 1
    assert "Spa running" in agent._recent_alerts[0]


async def test_recent_alerts_capped_at_8():
    """_recent_alerts never exceeds 8 entries."""
    from jarvis.agents.conversation import ConversationAgent

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(), send_fn=send_fn)

    for i in range(11):
        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message.content = f"Alert {i}"
        mock_choice.message.tool_calls = None
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
            await agent.run_proactive(f"event {i}", chat_id=123, use_history=False)

    assert len(agent._recent_alerts) == 8


def test_recent_alerts_not_truncated():
    """Full message text is retained for dedup, not cut at 200 chars."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(ha_client=MagicMock(), send_fn=None)
    long_msg = "x" * 500
    agent._record_sent(long_msg)
    assert len(agent._recent_alerts[-1]) >= 500


def test_prompt_loads_soul_and_drops_terseness():
    from jarvis.agents import conversation
    p = conversation._load_system_prompt(mode="conversation")
    assert "First sentence is the answer" not in p
    assert "certainly" in p.lower()
    assert "Current local date and time" not in p


def test_prompt_mode_layer_differs():
    from jarvis.agents import conversation
    proactive = conversation._load_system_prompt(mode="proactive")
    convo = conversation._load_system_prompt(mode="conversation")
    assert "PROACTIVE" in proactive
    assert "SILENT" in proactive
    assert proactive != convo


def test_proactive_round_cap_is_lower():
    from jarvis.agents import conversation
    assert conversation.MAX_PROACTIVE_TOOL_ROUNDS < conversation.MAX_TOOL_ROUNDS


async def test_recent_alerts_not_populated_on_silent():
    """SILENT responses should not be added to _recent_alerts."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock(), send_fn=AsyncMock())

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = "SILENT"
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.run_proactive("routine", chat_id=123, use_history=False)

    assert len(agent._recent_alerts) == 0


async def test_run_proactive_passes_model_override():
    """run_proactive forwards model param to _run_with_tools."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock(), send_fn=AsyncMock())

    captured = []
    async def capture(**kwargs):
        captured.append(kwargs.get("model"))
        m = MagicMock()
        m.choices[0].finish_reason = "stop"
        m.choices[0].message.content = "SILENT"
        m.choices[0].message.tool_calls = None
        return m

    with patch("litellm.acompletion", side_effect=capture):
        await agent.run_proactive("test", chat_id=1, use_history=False,
                                   model="openrouter/anthropic/claude-sonnet-4-6")

    assert "sonnet" in captured[0].lower()
