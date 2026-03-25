from __future__ import annotations

import asyncio
import datetime
import json
import logging
import zoneinfo
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent / "sunday_briefing_prompt.md"
MEMORY_PATH = Path(__file__).parent.parent / "memory.md"


def _fallback_prompt() -> str:
    from jarvis.config import config

    return (
        f"You are {config.BOT_NAME}, the AI for {config.OWNER_NAME}'s life.\n"
        "Generate a Sunday weekly briefing. Synthesise calendar, todos, weather, "
        "and goals into a concrete plan for the week. Be holistic — cover personal "
        "life, health, relationships, and fun, not just work. Under 600 words."
    )


def _load_system_prompt() -> str:
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text().strip()
    return _fallback_prompt()


def _load_goals() -> str:
    """Read memory.md for any stored goals, priorities, or life context."""
    if MEMORY_PATH.exists():
        return MEMORY_PATH.read_text().strip()
    return ""


async def _gather_calendar_events(ha_client: Any, days: int = 7) -> str:
    """Fetch upcoming events from all calendar entities in HA."""
    from jarvis.config import config

    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc

    now = datetime.datetime.now(tz)
    end = now + datetime.timedelta(days=days)
    start_str = now.strftime("%Y-%m-%dT%H:%M:%S")
    end_str = end.strftime("%Y-%m-%dT%H:%M:%S")

    calendar_entities = await ha_client.get_entities_by_domain("calendar")
    if not calendar_entities:
        return "No calendar entities found in Home Assistant."

    lines: list[str] = []
    for entity in calendar_entities:
        entity_id = entity["entity_id"]
        cal_name = entity.get("attributes", {}).get("friendly_name", entity_id)
        try:
            result = await ha_client.call_service(
                "calendar",
                "get_events",
                {"entity_id": entity_id, "start_date_time": start_str, "end_date_time": end_str},
            )
            # HA returns events in the response — format varies by integration
            events = _extract_events(result, entity_id)
            if events:
                lines.append(f"\n{cal_name}:")
                for ev in events:
                    summary = ev.get("summary", "untitled")
                    start = ev.get("start", "")
                    end_t = ev.get("end", "")
                    if start:
                        lines.append(f"  - {summary} ({start} → {end_t})")
                    else:
                        lines.append(f"  - {summary}")
            else:
                lines.append(f"\n{cal_name}: no events this week")
        except Exception as exc:
            logger.debug(f"Could not fetch events for {entity_id}: {exc}")
            lines.append(f"\n{cal_name}: unavailable ({exc})")

    return "\n".join(lines) if lines else "No calendar data available."


def _extract_events(service_response: Any, entity_id: str) -> list[dict]:
    """Extract event list from HA calendar.get_events response.

    The response format varies: sometimes it's a dict keyed by entity_id,
    sometimes a list of state-change dicts with events in attributes.
    """
    if isinstance(service_response, dict):
        # Direct dict: {entity_id: {"events": [...]}}
        entity_data = service_response.get(entity_id, service_response)
        if isinstance(entity_data, dict):
            return entity_data.get("events", [])
        return []

    if isinstance(service_response, list):
        for item in service_response:
            if isinstance(item, dict):
                # State-change format: [{"entity_id": ..., "attributes": {"events": [...]}}]
                if item.get("entity_id") == entity_id:
                    return item.get("attributes", {}).get("events", [])
                # Or the events might be directly in the list item
                if "events" in item:
                    return item["events"]
        # Maybe the list itself is the events
        if service_response and isinstance(service_response[0], dict) and "summary" in service_response[0]:
            return service_response

    return []


async def _gather_todos(ha_client: Any) -> str:
    """Fetch items from all todo list entities in HA."""
    todo_entities = await ha_client.get_entities_by_domain("todo")
    if not todo_entities:
        return "No todo list entities found in Home Assistant."

    lines: list[str] = []
    for entity in todo_entities:
        entity_id = entity["entity_id"]
        list_name = entity.get("attributes", {}).get("friendly_name", entity_id)
        try:
            result = await ha_client.call_service(
                "todo",
                "get_items",
                {"entity_id": entity_id},
            )
            items = _extract_todo_items(result, entity_id)
            if items:
                lines.append(f"\n{list_name}:")
                for item in items:
                    summary = item.get("summary", item.get("name", "untitled"))
                    status = item.get("status", "")
                    due = item.get("due", "")
                    marker = "x" if status == "completed" else " "
                    due_str = f" (due {due})" if due else ""
                    lines.append(f"  [{marker}] {summary}{due_str}")
            else:
                lines.append(f"\n{list_name}: empty")
        except Exception as exc:
            logger.debug(f"Could not fetch todos for {entity_id}: {exc}")
            lines.append(f"\n{list_name}: unavailable ({exc})")

    return "\n".join(lines) if lines else "No todo data available."


def _extract_todo_items(service_response: Any, entity_id: str) -> list[dict]:
    """Extract todo items from HA todo.get_items response."""
    if isinstance(service_response, dict):
        entity_data = service_response.get(entity_id, service_response)
        if isinstance(entity_data, dict):
            return entity_data.get("items", [])
        return []

    if isinstance(service_response, list):
        for item in service_response:
            if isinstance(item, dict):
                if item.get("entity_id") == entity_id:
                    return item.get("attributes", {}).get("items", [])
                if "items" in item:
                    return item["items"]
        if service_response and isinstance(service_response[0], dict) and "summary" in service_response[0]:
            return service_response

    return []


async def _gather_weather_forecast(ha_client: Any) -> str:
    """Fetch multi-day weather forecast from HA weather entities."""
    weather_entities = await ha_client.get_entities_by_domain("weather")
    if not weather_entities:
        return "No weather entities found in Home Assistant."

    lines: list[str] = []
    for entity in weather_entities:
        entity_id = entity["entity_id"]
        name = entity.get("attributes", {}).get("friendly_name", entity_id)
        current_state = entity.get("state", "unknown")
        temp = entity.get("attributes", {}).get("temperature", "?")
        temp_unit = entity.get("attributes", {}).get("temperature_unit", "")

        lines.append(f"\n{name}: currently {current_state}, {temp}{temp_unit}")

        try:
            result = await ha_client.call_service(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "daily"},
            )
            forecasts = _extract_forecasts(result, entity_id)
            for fc in forecasts[:7]:
                date = fc.get("datetime", fc.get("date", ""))
                if isinstance(date, str) and "T" in date:
                    date = date.split("T")[0]
                condition = fc.get("condition", "?")
                temp_high = fc.get("temperature", fc.get("templow", "?"))
                temp_low = fc.get("templow", "?")
                precip = fc.get("precipitation_probability", fc.get("precipitation", ""))
                precip_str = f", {precip}% rain" if precip not in ("", None) else ""
                lines.append(f"  {date}: {condition}, {temp_low}–{temp_high}{temp_unit}{precip_str}")
        except Exception as exc:
            logger.debug(f"Could not fetch forecast for {entity_id}: {exc}")
            # Fall back to attributes-based forecast if available
            forecast_attr = entity.get("attributes", {}).get("forecast", [])
            if forecast_attr:
                for fc in forecast_attr[:7]:
                    date = fc.get("datetime", "")
                    if isinstance(date, str) and "T" in date:
                        date = date.split("T")[0]
                    condition = fc.get("condition", "?")
                    lines.append(f"  {date}: {condition}")

    return "\n".join(lines) if lines else "No weather data available."


def _extract_forecasts(service_response: Any, entity_id: str) -> list[dict]:
    """Extract forecast list from HA weather.get_forecasts response."""
    if isinstance(service_response, dict):
        entity_data = service_response.get(entity_id, service_response)
        if isinstance(entity_data, dict):
            return entity_data.get("forecast", [])
        return []

    if isinstance(service_response, list):
        for item in service_response:
            if isinstance(item, dict):
                if item.get("entity_id") == entity_id:
                    return item.get("attributes", {}).get("forecast", [])
                if "forecast" in item:
                    return item["forecast"]

    return []


async def gather_context(ha_client: Any) -> str:
    """Gather all data sources concurrently and return formatted context."""
    from jarvis.config import config

    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc

    now = datetime.datetime.now(tz)
    date_header = now.strftime("Sunday %d %B %Y, %H:%M %Z")

    # Gather all data concurrently
    calendar_task = asyncio.create_task(_gather_calendar_events(ha_client))
    todo_task = asyncio.create_task(_gather_todos(ha_client))
    weather_task = asyncio.create_task(_gather_weather_forecast(ha_client))

    calendar_text, todo_text, weather_text = await asyncio.gather(
        calendar_task, todo_task, weather_task, return_exceptions=True
    )

    # Handle any exceptions from gather
    if isinstance(calendar_text, Exception):
        calendar_text = f"Calendar unavailable: {calendar_text}"
    if isinstance(todo_text, Exception):
        todo_text = f"Todos unavailable: {todo_text}"
    if isinstance(weather_text, Exception):
        weather_text = f"Weather unavailable: {weather_text}"

    goals_text = _load_goals()

    sections = [f"Sunday briefing request — {date_header}"]

    sections.append(f"\n--- CALENDAR (next 7 days) ---\n{calendar_text}")
    sections.append(f"\n--- TODO LISTS ---\n{todo_text}")
    sections.append(f"\n--- WEATHER FORECAST ---\n{weather_text}")

    if goals_text:
        sections.append(f"\n--- MEMORY & GOALS ---\n{goals_text}")
    else:
        sections.append(
            "\n--- MEMORY & GOALS ---\n"
            "No goals stored yet. Consider asking the user about their current "
            "priorities, health goals, and personal projects."
        )

    return "\n".join(sections)


async def generate(ha_client: Any) -> str:
    """Generate the Sunday weekly briefing."""
    from jarvis.router import complete

    context = await gather_context(ha_client)

    try:
        return await complete(
            "sunday_briefing",
            [
                {"role": "system", "content": _load_system_prompt()},
                {"role": "user", "content": context},
            ],
            max_tokens=1200,
            temperature=0.6,
        )
    except Exception as e:
        logger.error(f"Sunday briefing agent failed: {e}")
        return f"Good morning. (Sunday briefing unavailable: {e})"
