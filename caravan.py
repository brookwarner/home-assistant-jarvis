"""Caravan auto-heat control shared between the conversation tool and the scheduler.

The morning briefing (07:30) asks whether the caravan will be used that day. If the user
confirms, the conversation agent calls set_caravan(enabled=True); if they decline, it calls
set_caravan(enabled=False). If no explicit decision is made by the safety-net hour (default
09:00), the scheduler forces heating off so an unused caravan never heats.

The day-keyed decision flags below carry that state between those three call sites (all in
one process, so module globals are shared)."""
from __future__ import annotations
import datetime
import logging
import zoneinfo
from typing import Any

logger = logging.getLogger(__name__)

# Local date (YYYY-MM-DD) the caravan question was last asked / last explicitly decided.
_prompt_sent_day: str | None = None
_decided_day: str | None = None


def _today() -> str:
    from jarvis.config import config
    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc
    return datetime.datetime.now(tz).strftime("%Y-%m-%d")


def mark_prompt_sent() -> None:
    """Record that today's briefing asked the caravan question."""
    global _prompt_sent_day
    _prompt_sent_day = _today()


def mark_decided() -> None:
    """Record that the user made an explicit caravan decision today (on or off)."""
    global _decided_day
    _decided_day = _today()


def decision_pending() -> bool:
    """True if today's briefing asked the caravan question but no explicit decision has
    been recorded yet today — i.e. the safety net should force heating off."""
    today = _today()
    return _prompt_sent_day == today and _decided_day != today


def _domain(eid: str) -> str:
    return eid.split(".")[0] if "." in eid else ""


async def _read_float(ha_client: Any, entity_id: str) -> float | None:
    """Numeric state of an entity, or None if missing/unavailable/non-numeric."""
    if not entity_id:
        return None
    try:
        state = await ha_client.get_state(entity_id)
        return float((state or {}).get("state"))
    except Exception:
        return None


async def verify_drawing(ha_client: Any, send_fn: Any) -> dict:
    """Post-enable safety check: confirm the caravan is actually heating.

    Runs a few minutes after heating is enabled. Three outcomes:
      - master toggle never took (still off) -> re-enable + trigger, tell the user;
      - enabled, cold, but the heaters draw ~nothing -> switch the plugs on directly
        and warn (covers a dead plug or an automation that didn't fire);
      - enabled and drawing watts, or warm enough to be idle -> stay silent.

    This closes the gap where heating was reported "enabled" but the caravan stayed
    cold, which the HA-side power-draw validation can't catch (it only fires once a
    plug is already commanded on)."""
    from jarvis.config import config

    enable_eid = config.CARAVAN_ENTITIES[0] if config.CARAVAN_ENTITIES else None
    enabled = False
    if enable_eid:
        st = await ha_client.get_state(enable_eid)
        enabled = ((st or {}).get("state") or "").strip().lower() == "on"

    if not enabled:
        # The enable never stuck — re-run it for real so the caravan actually heats.
        await set_caravan(ha_client, enabled=True, trigger_now=True)
        await send_fn(
            "Heads up — I went to turn the caravan heat on but the toggle didn't "
            "stick the first time. I've switched it back on and kicked the heater off now."
        )
        return {"ok": False, "healed": True, "reason": "enable_did_not_stick"}

    temp = await _read_float(ha_client, config.CARAVAN_TEMP_SENSOR)
    watts = 0.0
    for sid in config.CARAVAN_POWER_SENSORS:
        watts += (await _read_float(ha_client, sid)) or 0.0

    drawing = watts >= config.CARAVAN_MIN_HEATER_WATTS
    cold = temp is not None and temp < config.CARAVAN_COMFORT_FLOOR_C

    if cold and not drawing:
        # Enabled and cold, but no power — switch the physical plugs on directly.
        for eid in config.CARAVAN_HEATER_SWITCHES:
            dom = _domain(eid) or "homeassistant"
            try:
                await ha_client.call_service(dom, "turn_on", {"entity_id": eid})
            except Exception as e:
                logger.debug(f"verify_drawing: turn_on {eid} failed: {e}")
        temp_s = f"{temp:g}°C" if temp is not None else "a low temperature"
        await send_fn(
            f"Caravan heat is on but the heaters were drawing no power at {temp_s} — "
            "I've switched the plugs on directly. Worth checking they're powered and on."
        )
        return {"ok": False, "healed": True, "reason": "no_draw", "temp": temp, "watts": watts}

    return {"ok": True, "healed": False, "enabled": enabled, "temp": temp, "watts": watts}


async def set_caravan(ha_client: Any, enabled: bool, trigger_now: bool = False) -> dict:
    """Switch every configured caravan entity on or off using its own domain
    (input_boolean.turn_on, automation.turn_off, switch.turn_on, ...). When enabling with
    trigger_now, also fire any automation entities so heating starts immediately rather
    than waiting for the automation's own trigger."""
    from jarvis.config import config
    entities = config.CARAVAN_ENTITIES
    if not entities:
        return {"error": "No caravan entities configured. Set CARAVAN_ENTITIES."}
    service = "turn_on" if enabled else "turn_off"
    results: list[dict] = []
    for eid in entities:
        dom = _domain(eid) or "homeassistant"  # homeassistant.turn_on works across domains
        try:
            await ha_client.call_service(dom, service, {"entity_id": eid})
            results.append({"entity_id": eid, "status": service})
        except Exception as e:
            results.append({"entity_id": eid, "error": str(e)})
    # When disabling, also force the physical heater plugs off now. The control entities
    # above (toggle + automation) only stop *future* auto-heat; a heater the automation
    # already switched on would otherwise keep running. We don't touch these when enabling —
    # the thermostat automation decides when to actually fire the heaters.
    if not enabled:
        for eid in config.CARAVAN_HEATER_SWITCHES:
            dom = _domain(eid) or "homeassistant"
            try:
                await ha_client.call_service(dom, "turn_off", {"entity_id": eid})
                results.append({"entity_id": eid, "status": "turn_off"})
            except Exception as e:
                results.append({"entity_id": eid, "error": str(e)})
    if enabled and trigger_now:
        for eid in entities:
            if _domain(eid) == "automation":
                try:
                    await ha_client.call_service("automation", "trigger", {"entity_id": eid})
                    results.append({"entity_id": eid, "status": "triggered"})
                except Exception as e:
                    results.append({"entity_id": eid, "error": f"trigger failed: {e}"})
    return {"enabled": enabled, "results": results}
