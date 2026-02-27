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
