import os
import pytest

def test_config_loads_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test_token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123456")
    monkeypatch.setenv("HA_TOKEN", "ha_test_token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    # Re-import to pick up monkeypatched env
    import importlib
    import jarvis.config as cfg_module
    importlib.reload(cfg_module)

    assert cfg_module.config.TELEGRAM_BOT_TOKEN == "test_token"
    assert cfg_module.config.TELEGRAM_CHAT_ID == 123456
    assert cfg_module.config.WEBHOOK_PORT == 8765


def test_proactive_model_default():
    from jarvis.config import config
    assert "sonnet" in config.PROACTIVE_MODEL.lower()


def test_voice_models_default_to_sonnet(monkeypatch):
    for v in ("BRIEFING_MODEL", "CONVERSATION_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    assert "sonnet" in c.config.BRIEFING_MODEL.lower()
    assert "sonnet" in c.config.CONVERSATION_MODEL.lower()


def test_proactive_toggles_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    monkeypatch.setenv("PROACTIVE_ENABLED", "false")
    monkeypatch.setenv("POLL_INTERVAL_MIN", "30")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    assert c.config.PROACTIVE_ENABLED is False
    assert c.config.POLL_INTERVAL_MIN == 30
