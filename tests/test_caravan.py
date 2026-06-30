import pytest
from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    for k, v in [("TELEGRAM_BOT_TOKEN", "t"), ("TELEGRAM_CHAT_ID", "1"),
                 ("HA_TOKEN", "h"), ("ANTHROPIC_API_KEY", "sk-ant-test")]:
        monkeypatch.setenv(k, v)


def _ha(states):
    """Fake HA client backed by a {entity_id: state_value} map."""
    ha = MagicMock()

    async def get_state(eid):
        if eid in states:
            return {"entity_id": eid, "state": states[eid]}
        return {"entity_id": eid, "state": "unknown"}

    ha.get_state = AsyncMock(side_effect=get_state)
    ha.call_service = AsyncMock(return_value=[])
    return ha


def _configure(config):
    config.CARAVAN_ENTITIES = [
        "input_boolean.caravan_heater_enabled",
        "automation.warm_caravan_2_minute_heartbeat",
    ]
    config.CARAVAN_HEATER_SWITCHES = ["switch.office_plug", "switch.caravan_plug"]
    config.CARAVAN_TEMP_SENSOR = "sensor.ths_caravan_temperature"
    config.CARAVAN_POWER_SENSORS = ["sensor.office_plug_power", "sensor.caravan_plug_power"]
    config.CARAVAN_MIN_HEATER_WATTS = 5.0
    config.CARAVAN_COMFORT_FLOOR_C = 16.0


async def test_verify_drawing_silent_when_heating_normally():
    """Enabled, cold, and the plugs are pulling watts — nothing to do, stay quiet."""
    from jarvis import caravan
    from jarvis.config import config
    _configure(config)
    ha = _ha({
        "input_boolean.caravan_heater_enabled": "on",
        "sensor.ths_caravan_temperature": "12.0",
        "sensor.office_plug_power": "780",
        "sensor.caravan_plug_power": "0",
    })
    send_fn = AsyncMock()

    result = await caravan.verify_drawing(ha, send_fn)

    assert result["ok"] is True
    send_fn.assert_not_awaited()
    # Did not touch the heater plugs.
    assert all(c.args[1] != "turn_on" for c in ha.call_service.await_args_list)


async def test_verify_drawing_selfheals_when_enabled_cold_but_no_draw():
    """Enabled and cold but drawing ~nothing: switch the plugs on directly and warn."""
    from jarvis import caravan
    from jarvis.config import config
    _configure(config)
    ha = _ha({
        "input_boolean.caravan_heater_enabled": "on",
        "sensor.ths_caravan_temperature": "11.6",
        "sensor.office_plug_power": "0",
        "sensor.caravan_plug_power": "0",
    })
    send_fn = AsyncMock()

    result = await caravan.verify_drawing(ha, send_fn)

    assert result["healed"] is True
    on_targets = {c.args[2]["entity_id"] for c in ha.call_service.await_args_list if c.args[1] == "turn_on"}
    assert on_targets == {"switch.office_plug", "switch.caravan_plug"}
    send_fn.assert_awaited_once()
    assert "power" in send_fn.await_args.args[0].lower()


async def test_verify_drawing_reenables_when_enable_did_not_stick():
    """The enable never took (toggle still off): re-enable + trigger and tell the user.
    This is the failure that left the caravan cold — the tool never flipped the toggle."""
    from jarvis import caravan
    from jarvis.config import config
    _configure(config)
    ha = _ha({
        "input_boolean.caravan_heater_enabled": "off",
        "sensor.ths_caravan_temperature": "11.6",
        "sensor.office_plug_power": "0",
        "sensor.caravan_plug_power": "0",
    })
    send_fn = AsyncMock()

    result = await caravan.verify_drawing(ha, send_fn)

    assert result["healed"] is True
    # Re-enabled the master toggle.
    on_targets = {c.args[2]["entity_id"] for c in ha.call_service.await_args_list if c.args[1] == "turn_on"}
    assert "input_boolean.caravan_heater_enabled" in on_targets
    send_fn.assert_awaited_once()
    assert "didn't" in send_fn.await_args.args[0].lower() or "did not" in send_fn.await_args.args[0].lower()


async def test_verify_drawing_silent_when_warm_and_idle():
    """Enabled, warm enough, plugs legitimately idle — no fault, stay quiet."""
    from jarvis import caravan
    from jarvis.config import config
    _configure(config)
    ha = _ha({
        "input_boolean.caravan_heater_enabled": "on",
        "sensor.ths_caravan_temperature": "21.0",
        "sensor.office_plug_power": "0",
        "sensor.caravan_plug_power": "0",
    })
    send_fn = AsyncMock()

    result = await caravan.verify_drawing(ha, send_fn)

    assert result["ok"] is True
    send_fn.assert_not_awaited()
