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
    # Proactive polling defaults to Haiku to keep unattended cost low.
    assert "haiku" in config.PROACTIVE_MODEL.lower()


def test_voice_models_default_to_haiku(monkeypatch):
    for v in ("BRIEFING_MODEL", "CONVERSATION_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    assert "haiku" in c.config.BRIEFING_MODEL.lower()
    assert "haiku" in c.config.CONVERSATION_MODEL.lower()


def test_default_models_bill_anthropic_directly(monkeypatch):
    for v in ("TRIAGE_MODEL", "BRIEFING_MODEL", "CONVERSATION_MODEL", "PROACTIVE_MODEL", "OPUS_MODEL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    for m in (c.config.TRIAGE_MODEL, c.config.BRIEFING_MODEL, c.config.CONVERSATION_MODEL,
              c.config.PROACTIVE_MODEL, c.config.OPUS_MODEL):
        assert m.startswith("anthropic/"), m


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


def test_timezone_blank_by_default(monkeypatch):
    for v in ("TIMEZONE",):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    assert c.config.TIMEZONE == ""  # blank => follow HA at startup


def test_briefing_excludes_nz_average_sensor_by_default(monkeypatch):
    """The hardcoded 'Water Usage vs NZ Average' template sensor is excluded from the
    briefing by default — it compares to a fixed 200 L/day constant and drove a stale,
    repetitive '174%' headline morning after morning."""
    for v in ("BRIEFING_EXCLUDE_ENTITIES",):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    assert "sensor.water_usage_vs_average" in c.config.BRIEFING_EXCLUDE_ENTITIES


def test_briefing_exclude_overridable_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("HA_TOKEN", "h")
    monkeypatch.setenv("BRIEFING_EXCLUDE_ENTITIES", "sensor.foo, sensor.bar")
    import importlib
    import jarvis.config as c
    importlib.reload(c)
    assert c.config.BRIEFING_EXCLUDE_ENTITIES == ["sensor.foo", "sensor.bar"]


def test_quiet_window_defaults(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "HA_TOKEN": "h",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PROACTIVE_QUIET_START", raising=False)
    monkeypatch.delenv("PROACTIVE_QUIET_END", raising=False)
    import importlib
    from jarvis import config as cfgmod
    importlib.reload(cfgmod)
    assert cfgmod.config.PROACTIVE_QUIET_START == 23
    assert cfgmod.config.PROACTIVE_QUIET_END == 6
