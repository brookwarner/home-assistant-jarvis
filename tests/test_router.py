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

    call_kwargs = mock_ac.call_args[1]
    assert "llama" in call_kwargs["model"] or "openrouter" in call_kwargs["model"]
