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
    if enabled and trigger_now:
        for eid in entities:
            if _domain(eid) == "automation":
                try:
                    await ha_client.call_service("automation", "trigger", {"entity_id": eid})
                    results.append({"entity_id": eid, "status": "triggered"})
                except Exception as e:
                    results.append({"entity_id": eid, "error": f"trigger failed: {e}"})
    return {"enabled": enabled, "results": results}
