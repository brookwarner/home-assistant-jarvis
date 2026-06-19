import pytest
from unittest.mock import AsyncMock, patch, MagicMock

pytestmark = pytest.mark.asyncio

@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "ha_token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

async def test_complete_returns_string(mock_env):
    from jarvis.router import complete

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "test response"

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp):
        result = await complete("triage", [{"role": "user", "content": "hello"}])

    assert result == "test response"

async def test_triage_model_uses_configured_model(mock_env):
    from jarvis import router
    import importlib
    importlib.reload(router)

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "ignore"

    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_ac:
        await router.complete("triage", [{"role": "user", "content": "test"}])

    from jarvis.config import config
    call_kwargs = mock_ac.call_args[1]
    # Triage uses whatever TRIAGE_MODEL resolves to (defaults to direct-Anthropic Haiku).
    assert call_kwargs["model"] == config.TRIAGE_MODEL


from jarvis.router import build_cached_messages


def test_build_cached_messages_marks_system_for_anthropic():
    msgs = [
        {"role": "system", "content": "BIG STATIC PROMPT"},
        {"role": "user", "content": "hi"},
    ]
    out = build_cached_messages(msgs, "anthropic/claude-haiku-4-5")
    assert out[0]["content"] == [
        {"type": "text", "text": "BIG STATIC PROMPT",
         "cache_control": {"type": "ephemeral"}}
    ]
    assert out[1] == {"role": "user", "content": "hi"}
    assert msgs[0]["content"] == "BIG STATIC PROMPT"


def test_build_cached_messages_noop_for_non_anthropic():
    msgs = [{"role": "system", "content": "X"}, {"role": "user", "content": "y"}]
    out = build_cached_messages(msgs, "openrouter/anthropic/claude-4.6-sonnet")
    assert out == msgs


def test_build_cached_messages_no_system_is_noop():
    msgs = [{"role": "user", "content": "y"}]
    out = build_cached_messages(msgs, "anthropic/claude-haiku-4-5")
    assert out == msgs


async def test_complete_passes_cached_messages_for_anthropic(mock_env, monkeypatch):
    monkeypatch.setenv("BRIEFING_MODEL", "anthropic/claude-haiku-4-5")
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
    from jarvis import router
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_ac:
        await router.complete("briefing", [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
        ])
    sent = mock_ac.call_args.kwargs["messages"]
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
