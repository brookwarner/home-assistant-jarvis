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

    result = await agent._execute_tool("send_message", {"text": "Hello there"})

    send_fn.assert_awaited_once_with("Hello there")
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

async def test_set_caravan_heating_enables_each_entity_by_domain():
    """enabled=true turns on every configured entity using its own domain."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config

    config.CARAVAN_ENTITIES = [
        "input_boolean.caravan_heater_enabled", "automation.warm_caravan_on_cold_workdays",
    ]
    config.CARAVAN_HEATER_SWITCHES = ["switch.oil_heater", "switch.fan_heater"]
    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])
    agent = ConversationAgent(mock_ha)

    result = await agent._execute_tool("set_caravan_heating", {"enabled": True})

    assert result["enabled"] is True
    calls = mock_ha.call_service.await_args_list
    # Each control entity switched on via its own domain. The physical heater switches are
    # left untouched on enable — the thermostat automation decides when to fire them.
    turn_ons = {(c.args[0], c.args[2]["entity_id"]) for c in calls if c.args[1] == "turn_on"}
    assert turn_ons == {
        ("input_boolean", "input_boolean.caravan_heater_enabled"),
        ("automation", "automation.warm_caravan_on_cold_workdays"),
    }


async def test_enabling_caravan_triggers_heat_immediately_by_default():
    """Enabling fires the auto-heat automation now (trigger_now defaults True) so heat
    starts on the next heartbeat instead of waiting, and the user isn't relying on the
    automation re-evaluating on its own."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config

    config.CARAVAN_ENTITIES = [
        "input_boolean.caravan_heater_enabled", "automation.warm_caravan_on_cold_workdays",
    ]
    config.CARAVAN_HEATER_SWITCHES = []
    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])
    agent = ConversationAgent(mock_ha)  # no send_fn → no async verification task

    await agent._execute_tool("set_caravan_heating", {"enabled": True})

    triggers = [c for c in mock_ha.call_service.await_args_list if c.args[1] == "trigger"]
    assert len(triggers) == 1
    assert triggers[0].args[2]["entity_id"] == "automation.warm_caravan_on_cold_workdays"


async def test_enabling_caravan_arms_power_verification(monkeypatch):
    """After enabling, the agent schedules a delayed power-draw verification so a silent
    failure (toggle didn't take / heaters not drawing) gets caught and self-healed."""
    import asyncio
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config

    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    config.CARAVAN_HEATER_SWITCHES = []
    config.CARAVAN_VERIFY_DELAY_MIN = 0  # run effectively immediately in the test
    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])

    called = asyncio.Event()

    async def fake_verify(ha_client, send_fn):
        called.set()
        return {"ok": True}

    monkeypatch.setattr("jarvis.caravan.verify_drawing", fake_verify)
    agent = ConversationAgent(mock_ha, send_fn=AsyncMock())

    await agent._execute_tool("set_caravan_heating", {"enabled": True})
    await asyncio.wait_for(called.wait(), timeout=2)
    assert called.is_set()


async def test_disabling_cancels_pending_verification(monkeypatch):
    """If the user enables then changes their mind and disables within the verify window,
    the pending power-draw check must be cancelled — otherwise it would see 'off' and
    wrongly re-enable the heat against their wishes."""
    import asyncio
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config

    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    config.CARAVAN_HEATER_SWITCHES = []
    config.CARAVAN_VERIFY_DELAY_MIN = 0.02  # ~1.2s — long enough to cancel before it fires
    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])

    verify_calls = []

    async def fake_verify(ha_client, send_fn):
        verify_calls.append(True)
        return {"ok": True}

    monkeypatch.setattr("jarvis.caravan.verify_drawing", fake_verify)
    agent = ConversationAgent(mock_ha, send_fn=AsyncMock())

    await agent._execute_tool("set_caravan_heating", {"enabled": True})
    task = agent._caravan_verify_task
    assert task is not None
    await agent._execute_tool("set_caravan_heating", {"enabled": False})
    await asyncio.sleep(0)  # let the cancellation settle
    assert task.cancelled() or task.done()
    assert verify_calls == []


def test_operational_prompt_forbids_claiming_untaken_caravan_action():
    """Root cause of the cold caravan: the model SAID 'heating enabled' but never called
    the tool. The prompt must forbid claiming a caravan action without actually calling
    set_caravan_heating in the same turn."""
    from jarvis.agents.conversation import _operational_layer
    p = _operational_layer().lower()
    assert "actually called set_caravan_heating" in p


async def test_disabling_caravan_does_not_arm_verification(monkeypatch):
    """Disabling must not schedule a power-draw check — there's nothing to verify."""
    import asyncio
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config

    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    config.CARAVAN_HEATER_SWITCHES = []
    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])

    verify_calls = []

    async def fake_verify(ha_client, send_fn):
        verify_calls.append(True)
        return {"ok": True}

    monkeypatch.setattr("jarvis.caravan.verify_drawing", fake_verify)
    agent = ConversationAgent(mock_ha, send_fn=AsyncMock())

    await agent._execute_tool("set_caravan_heating", {"enabled": False})
    await asyncio.sleep(0.05)
    assert verify_calls == []


async def test_set_caravan_heating_disables_and_can_trigger():
    """enabled=false turns off; trigger_now also fires only the automation entities."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config

    config.CARAVAN_ENTITIES = [
        "input_boolean.caravan_heater_enabled", "automation.warm_caravan_on_cold_workdays",
    ]
    config.CARAVAN_HEATER_SWITCHES = ["switch.oil_heater", "switch.fan_heater"]
    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])
    agent = ConversationAgent(mock_ha)

    off = await agent._execute_tool("set_caravan_heating", {"enabled": False})
    assert off["enabled"] is False
    assert {c.args[1] for c in mock_ha.call_service.await_args_list} == {"turn_off"}
    # Disabling also force-switches the physical heater plugs off, not just the controls.
    off_targets = {c.args[2]["entity_id"] for c in mock_ha.call_service.await_args_list}
    assert "switch.oil_heater" in off_targets and "switch.fan_heater" in off_targets

    mock_ha.call_service.reset_mock()
    on = await agent._execute_tool("set_caravan_heating", {"enabled": True, "trigger_now": True})
    triggers = [c for c in mock_ha.call_service.await_args_list if c.args[1] == "trigger"]
    assert len(triggers) == 1  # only the automation is triggered, not the input_boolean
    assert triggers[0].args[2]["entity_id"] == "automation.warm_caravan_on_cold_workdays"
    assert on["enabled"] is True


async def test_set_caravan_heating_records_decision():
    """Calling the tool marks today's caravan decision so the safety net stands down."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient
    from jarvis.config import config
    from jarvis import caravan

    config.CARAVAN_ENTITIES = ["input_boolean.caravan_heater_enabled"]
    caravan.mark_prompt_sent()
    assert caravan.decision_pending() is True

    mock_ha = MagicMock(spec=HAClient)
    mock_ha.call_service = AsyncMock(return_value=[])
    agent = ConversationAgent(mock_ha)
    await agent._execute_tool("set_caravan_heating", {"enabled": True})

    assert caravan.decision_pending() is False


def test_note_briefing_records_user_then_assistant_turn():
    """note_briefing appends a user trigger turn + assistant text so history stays valid."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    agent = ConversationAgent(MagicMock(spec=HAClient))
    agent.note_briefing(123, "Good morning. Will you use the caravan today?")

    history = agent._history[123]
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["role"] == "assistant"
    assert "caravan" in history[1]["content"].lower()


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


async def test_run_proactive_strips_reasoning_before_notify():
    """On the notify path, the model's deliberation before NOTIFY: must not reach the user —
    only the clean message after the marker is sent."""
    from jarvis.agents.conversation import ConversationAgent

    send_fn = AsyncMock()
    agent = ConversationAgent(MagicMock(), send_fn=send_fn)

    mock_choice = MagicMock()
    mock_choice.finish_reason = "stop"
    mock_choice.message.content = (
        "I need to assess whether this warrants interrupting the user. "
        "The spa thermostat is one of two open issues.\n"
        "NOTIFY:\n"
        "The spa thermostat (ITC-308-WIFI) has dropped off WiFi and is unavailable."
    )
    mock_choice.message.tool_calls = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_response):
        await agent.run_proactive("itc_308: online -> unavailable", chat_id=123)

    send_fn.assert_awaited_once_with(
        "The spa thermostat (ITC-308-WIFI) has dropped off WiFi and is unavailable."
    )


def test_finalize_proactive_paths():
    """_finalize_proactive: SILENT and empty -> None; NOTIFY: -> clean message; bare -> as-is."""
    from jarvis.agents import conversation as c

    assert c._finalize_proactive("SILENT") is None
    assert c._finalize_proactive("reasoning\nNOTIFY:") is None  # empty message -> silent
    assert c._finalize_proactive("why I'll speak\nNOTIFY:\nHi there") == "Hi there"
    assert c._finalize_proactive("NOTIFY: inline message") == "inline message"
    assert c._finalize_proactive("plain text, no marker") == "plain text, no marker"


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


async def test_check_anomalies_tool_returns_anomalies(monkeypatch):
    """check_anomalies wraps anomaly.detect_and_surface and returns its descriptors."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock())

    async def fake_detect(ha):
        return ["water: 900 L yesterday vs ~300 L typical (+600 L, 3.0x)"]

    monkeypatch.setattr("jarvis.anomaly.detect_and_surface", fake_detect)
    result = await agent._execute_tool("check_anomalies", {})

    assert result["anomalies"]
    assert "water" in result["anomalies"][0]


async def test_check_anomalies_tool_empty(monkeypatch):
    """check_anomalies returns an explanatory note when nothing deviates."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock())

    async def fake_detect(ha):
        return []

    monkeypatch.setattr("jarvis.anomaly.detect_and_surface", fake_detect)
    result = await agent._execute_tool("check_anomalies", {})

    assert result["anomalies"] == []
    assert "note" in result


def test_collect_changes_mtime_fallback(tmp_path):
    """Non-git dir falls back to recently-modified files, skipping junk."""
    from jarvis.agents import conversation

    (tmp_path / "a.txt").write_text("hi")
    (tmp_path / "b.py").write_text("x = 1")
    junk = tmp_path / "__pycache__"
    junk.mkdir()
    (junk / "x.pyc").write_text("nope")

    result = conversation._collect_changes(tmp_path, 10)

    assert result["source"] == "mtime"
    files = [f["file"] for f in result["recent_files"]]
    assert "a.txt" in files and "b.py" in files
    assert not any("x.pyc" in f for f in files)


def test_collect_changes_uses_git(tmp_path):
    """A real git repo returns commit log, not mtime."""
    import shutil
    import subprocess as sp

    if not shutil.which("git"):
        pytest.skip("git not available")

    sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
    sp.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, capture_output=True)
    sp.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
    (tmp_path / "f.txt").write_text("hi")
    sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    sp.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, capture_output=True)

    from jarvis.agents import conversation
    result = conversation._collect_changes(tmp_path, 5)

    assert result["source"] == "git"
    assert any("initial commit" in c for c in result["commits"])


async def test_recent_changes_tool_runs_against_repo():
    """recent_changes tool returns a result via either git or mtime."""
    from jarvis.agents.conversation import ConversationAgent

    agent = ConversationAgent(MagicMock())
    result = await agent._execute_tool("recent_changes", {})

    assert result["source"] in ("git", "mtime")


async def test_current_mode_injected_into_reply(monkeypatch):
    """The active operating mode is injected into the per-call user message."""
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    agent = ConversationAgent(MagicMock(spec=HAClient))
    monkeypatch.setattr("jarvis.scheduler.resolve_mode", AsyncMock(return_value="away"))

    captured = {}

    async def cap(**kwargs):
        captured["messages"] = kwargs["messages"]
        m = MagicMock()
        m.choices[0].finish_reason = "stop"
        m.choices[0].message.content = "ok"
        m.choices[0].message.tool_calls = None
        return m

    with patch("litellm.acompletion", side_effect=cap):
        await agent.reply(chat_id=5, user_text="hi")

    user_msgs = [m for m in captured["messages"] if m["role"] == "user"]
    assert any("away" in (m["content"] or "") for m in user_msgs)


def test_operational_layer_describes_new_features():
    """The static prompt advertises modes, anomaly detection, and recent_changes."""
    from jarvis.agents import conversation
    op = conversation._operational_layer()
    assert "mode" in op.lower()
    assert "check_anomalies" in op
    assert "recent_changes" in op


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


async def test_run_with_tools_caches_system_prefix(mock_env, monkeypatch):
    monkeypatch.setenv("CONVERSATION_MODEL", "anthropic/claude-haiku-4-5")
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    mock_ha = MagicMock(spec=HAClient)
    agent = ConversationAgent(mock_ha)
    agent._model = "anthropic/claude-haiku-4-5"

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(
        finish_reason="stop",
        message=MagicMock(content="done", tool_calls=None),
    )]
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_ac:
        await agent._run_with_tools([{"role": "user", "content": "hi"}])

    sent = mock_ac.call_args.kwargs["messages"]
    assert sent[0]["role"] == "system"
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


async def test_run_opus_system_prompt_is_static_for_caching(mock_env, monkeypatch):
    """_run_opus must NOT embed a timestamp in the system prompt (cache killer).

    The volatile now-stamp must ride on the user turn instead, keeping the
    cached system prefix byte-stable across calls.
    """
    monkeypatch.setenv("OPUS_MODEL", "anthropic/claude-opus-4-6")
    from jarvis.agents.conversation import ConversationAgent
    from jarvis.ha_client import HAClient

    mock_ha = MagicMock(spec=HAClient)
    agent = ConversationAgent(mock_ha)

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(
        finish_reason="stop",
        message=MagicMock(content="done", tool_calls=None),
    )]
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_ac:
        await agent._run_opus("refactor the thing")

    sent = mock_ac.call_args.kwargs["messages"]
    # System block may be a string or a list of content dicts (from build_cached_messages)
    system_content = sent[0]["content"]
    if isinstance(system_content, list):
        system_text = " ".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in system_content
        )
    else:
        system_text = system_content

    assert "Current local date and time" not in system_text, (
        "Timestamp in system prompt invalidates the cache on every call"
    )
    # The volatile now-stamp must ride on the user turn instead
    assert any(
        "(now:" in (m.get("content") or "")
        for m in sent
        if m["role"] == "user"
    ), "Expected '(now:' timestamp in user message"
