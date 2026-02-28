from __future__ import annotations
import asyncio
import json
import logging
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any
import litellm

logger = logging.getLogger(__name__)


def _tz() -> str:
    from jarvis.config import config
    return config.TIMEZONE


def _bot_name() -> str:
    from jarvis.config import config
    return config.BOT_NAME

MAX_HISTORY = 20
MAX_TOOL_ROUNDS = 5

SOUL_PATH = Path(__file__).parent.parent / "soul.md"
ENTITIES_PATH = Path(__file__).parent.parent / "ha_entities.md"
MEMORY_PATH = Path(__file__).parent.parent / "memory.md"
BRIEFING_PROMPT_PATH = Path(__file__).parent.parent / "briefing_prompt.md"

SELF_EDIT_FILES = {
    "soul.md": SOUL_PATH,
    "ha_entities.md": ENTITIES_PATH,
    "briefing_prompt.md": BRIEFING_PROMPT_PATH,
}

ALLOWED_CONFIG_FILES = {
    "automations.yaml",
    "configuration.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "sensors.yaml",
}

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_state",
            "description": "Get the current state of a single Home Assistant entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string", "description": "e.g. sensor.attic_temperature"}
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_states_by_domain",
            "description": "Get all entity states for a domain (e.g. 'switch', 'sensor', 'light').",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "e.g. switch, sensor, light, climate"}
                },
                "required": ["domain"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "call_service",
            "description": "Call a Home Assistant service to control a device.",
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "e.g. switch, light, climate"},
                    "service": {"type": "string", "description": "e.g. turn_on, turn_off, set_temperature"},
                    "entity_id": {"type": "string", "description": "Target entity"},
                    "extra_data": {"type": "object", "description": "Additional service data (optional)"},
                },
                "required": ["domain", "service", "entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history",
            "description": "Get recent state history for an entity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "hours": {"type": "integer", "description": "How many hours back (default 24)"},
                },
                "required": ["entity_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_statistics",
            "description": (
                "Search for available long-term statistic IDs by keyword. "
                "Use this before get_statistics to discover the correct statistic_id. "
                "Examples: 'energy', 'spa', 'water', 'cost', 'temperature', 'meridian', 'watercare'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search for"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statistics",
            "description": (
                "Fetch long-term statistics from HA's recorder database. "
                "Use search_statistics first to find the correct statistic_id. "
                "Returns total usage over the window plus a daily breakdown. "
                "For 'this month' use hours=672. For 'today' use hours=24. For 'this week' use hours=168."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "statistic_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of statistic IDs, e.g. ['meridian_energy:consumption_day']",
                    },
                    "period": {
                        "type": "string",
                        "enum": ["5minute", "hour", "day", "week", "month"],
                        "description": "Aggregation period (default: hour)",
                    },
                    "hours": {
                        "type": "integer",
                        "description": "How many hours of history to fetch (default 48)",
                    },
                },
                "required": ["statistic_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": (
                "Search the known entity reference by keyword. "
                "Use this to find the correct entity_id before calling get_state or call_service. "
                "Examples: 'temperature', 'spa', 'door', 'energy', 'weather', 'fan'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword to search for (e.g. 'spa', 'lounge', 'attic')"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_custom_alert",
            "description": "Add a new custom monitor that will be checked every 5 minutes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_id": {"type": "string"},
                    "condition": {"type": "string", "enum": ["above", "below", "equals"]},
                    "threshold": {"type": "number"},
                    "message": {"type": "string", "description": "Message to send when triggered"},
                },
                "required": ["entity_id", "condition", "threshold", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save a fact, preference, or instruction to persistent memory for use in future conversations. "
                "Use whenever the user tells you something they want you to remember."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "What to remember (e.g. 'User prefers spa at 38C')"},
                },
                "required": ["note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegate_to_opus",
            "description": (
                "Hand a complex task to the Opus sub-agent (Claude Opus 4.6). "
                "Use for: big refactors, multi-file HA config changes, writing new automations, "
                "debugging complex issues, or anything requiring deep reasoning. "
                "Opus has the same tools you do but is much smarter and more expensive. "
                "Only delegate when the task genuinely warrants it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Clear description of what Opus should do, with full context",
                    }
                },
                "required": ["task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_self",
            "description": (
                "Read one of the bot's own configuration files. "
                "Available: soul.md (personality), ha_entities.md (known entities), briefing_prompt.md (morning briefing instructions)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "enum": ["soul.md", "ha_entities.md", "briefing_prompt.md"],
                    }
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_self",
            "description": (
                "Overwrite one of the bot's own configuration files. "
                "Changes to soul.md and briefing_prompt.md take effect on the next message. "
                "Always read_self first. Available: soul.md, ha_entities.md, briefing_prompt.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "enum": ["soul.md", "ha_entities.md", "briefing_prompt.md"],
                    },
                    "content": {"type": "string", "description": "Complete new file content"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_ha_config",
            "description": "Read a Home Assistant configuration file. Use before editing automations, scripts, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "e.g. automations.yaml, configuration.yaml, scripts.yaml",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_ha_config",
            "description": (
                "Overwrite a Home Assistant configuration file with new content. "
                "Runs 'ha core check' to validate before saving. Backs up and restores on failure. "
                "Always read_ha_config first to avoid losing existing content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "e.g. automations.yaml"},
                    "content": {"type": "string", "description": "Complete file content to write"},
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reload_ha_config",
            "description": "Reload HA automations/scripts/scenes after editing. Call after write_ha_config.",
            "parameters": {
                "type": "object",
                "properties": {
                    "component": {
                        "type": "string",
                        "enum": ["automation", "script", "scene"],
                        "description": "Which component to reload",
                    }
                },
                "required": ["component"],
            },
        },
    },
]


def _now_str() -> str:
    """Current local date/time as a readable string."""
    import datetime, zoneinfo
    tz_name = _tz()
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        tz = datetime.timezone.utc
    now = datetime.datetime.now(tz)
    return now.strftime("%A %d %B %Y, %H:%M %Z")


def _load_system_prompt() -> str:
    """Load system prompt fresh each call so memory and entity updates are picked up."""
    base = (
        f"Current local date and time: {_now_str()}\n\n"
        "You have tools to read entity states, control devices, remember things, and edit HA config files.\n"
        "To find entity IDs: use search_entities with a broad keyword. "
        "If search_entities returns nothing, try a different keyword, then try get_states_by_domain, then try get_state with a guessed ID. "
        "Never give up after one failed search — try at least 3 approaches.\n"
        "When taking actions, confirm what you did in one sentence.\n"
        "When asked questions, fetch live data — never guess entity IDs without trying.\n\n"
        f"TIMEZONE: All HA timestamps are UTC. Local timezone is {_tz()}. Always convert to local time before reporting.\n\n"
        "FORMATTING: Never use markdown. No bold, italics, tables, * bullets, # headers, backticks.\n\n"
        "BREVITY: First sentence is the answer. Add context only if essential. "
        "Never say 'certainly', 'of course', 'happy to help', 'great question'. Just answer."
    )

    memory = ""
    if MEMORY_PATH.exists():
        mem = MEMORY_PATH.read_text().strip()
        if mem:
            memory = f"\n\nYour persistent memory notes:\n{mem}"

    if SOUL_PATH.exists():
        soul = SOUL_PATH.read_text()
        return f"{soul}\n\n---\n\n{base}{memory}"
    return (
        f"You are {_bot_name()}, an AI smart home assistant.\n\n"
        + base + memory
    )


_HUMAN_SERVICE = {"turn_on": "on", "turn_off": "off", "toggle": "toggled"}


def _format_tool_footer(tool_log: list[tuple[str, dict]]) -> str:
    """Compact footer showing what the bot actually did. No raw entity IDs."""
    reads = 0
    actions: list[str] = []

    for name, inputs in tool_log:
        if name in ("get_state", "get_states_by_domain", "get_history", "get_statistics", "search_statistics", "read_ha_config", "read_self", "search_entities"):
            reads += 1
        elif name == "call_service":
            svc = inputs.get("service", "?")
            label = _HUMAN_SERVICE.get(svc, svc)
            domain = inputs.get("domain", "")
            actions.append(f"{domain} {label}")
        elif name == "write_ha_config":
            actions.append(f"wrote {inputs.get('filename', '?')}")
        elif name == "reload_ha_config":
            actions.append(f"reloaded {inputs.get('component', '?')}")
        elif name == "remember":
            actions.append("saved to memory")
        elif name == "write_self":
            actions.append(f"edited {inputs.get('filename', '?')}")
        elif name == "delegate_to_opus":
            actions.append("delegated to Opus")
        elif name == "add_custom_alert":
            actions.append("added alert")

    parts: list[str] = []
    if reads > 0:
        parts.append(f"checked {reads} source{'s' if reads > 1 else ''}")
    parts.extend(actions)

    return "\n\n(" + ", ".join(parts) + ")" if parts else ""


class ConversationAgent:
    def __init__(self, ha_client: Any, send_fn=None):
        from jarvis.config import config
        self._ha = ha_client
        self._model = config.CONVERSATION_MODEL
        self._history: dict[int, list[dict]] = defaultdict(list)
        self._send_fn = send_fn          # async (text: str) -> None
        self._pending_reply: asyncio.Future | None = None
        self._agent_busy = False

    async def reply(self, chat_id: int, user_text: str) -> str:
        history = self._history[chat_id]
        history.append({"role": "user", "content": user_text})

        if len(history) > MAX_HISTORY:
            history[:] = history[-MAX_HISTORY:]

        try:
            response_text = await self._run_with_tools(history)
            history.append({"role": "assistant", "content": response_text})
            return response_text
        except Exception as e:
            logger.error(f"Conversation agent failed: {e}")
            return f"Error: {e}"

    async def _run_with_tools(self, messages: list[dict]) -> str:
        # Reload system prompt each call so memory/entity changes are live
        msgs = [{"role": "system", "content": _load_system_prompt()}] + messages
        tool_log: list[tuple[str, dict]] = []
        rounds = 0

        while rounds < MAX_TOOL_ROUNDS:
            extra: dict = {}
            if self._model.startswith("openrouter/"):
                from jarvis.config import config
                if config.OPENROUTER_API_KEY:
                    extra["api_key"] = config.OPENROUTER_API_KEY

            response = await litellm.acompletion(
                model=self._model,
                messages=msgs,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.5,
                max_tokens=1024,
                **extra,
            )

            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                rounds += 1
                msgs.append({
                    "role": "assistant",
                    "content": msg.content,
                    "tool_calls": msg.tool_calls,
                })
                for tc in msg.tool_calls:
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except Exception:
                        inputs = {}
                    tool_log.append((tc.function.name, inputs))
                    result = await self._execute_tool(tc.function.name, inputs)
                    msgs.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(result),
                    })
            else:
                content = msg.content
                # If model returned empty content (no answer), force a synthesis
                if not content or not content.strip():
                    msgs.append({"role": "assistant", "content": None})
                    msgs.append({"role": "user", "content": "Based on everything you found, give your answer now."})
                    retry = await litellm.acompletion(
                        model=self._model, messages=msgs, temperature=0.5, max_tokens=1024, **extra,
                    )
                    content = retry.choices[0].message.content or "I checked but couldn't formulate a response."
                return content + _format_tool_footer(tool_log)

        # Hit max tool rounds — force a final response without tools
        msgs.append({"role": "user", "content": "Based on everything you found, give your answer now."})
        response = await litellm.acompletion(
            model=self._model, messages=msgs, temperature=0.5, max_tokens=1024, **extra,
        )
        content = response.choices[0].message.content or "I checked but couldn't formulate a response."
        return content + _format_tool_footer(tool_log)

    async def _run_opus(self, task: str) -> dict:
        """Run a task using the Opus sub-agent with the same tools."""
        from jarvis.config import config
        logger.info(f"Delegating to Opus: {task[:80]}")

        opus_system = (
            f"You are {_bot_name()}-Opus, the heavy-duty sub-agent for a Home Assistant smart home.\n"
            "You handle complex tasks: refactors, multi-file edits, debugging, new automations.\n"
            "You have the same tools as the main agent. Work carefully, verify your changes.\n"
            "Return a clear summary of what you did.\n\n"
            f"Current local date and time: {_now_str()}\n"
        f"TIMEZONE: All HA timestamps are UTC. Local timezone is {_tz()}. Convert all times.\n"
            "FORMATTING: Plain text only. No markdown."
        )
        msgs = [
            {"role": "system", "content": opus_system},
            {"role": "user", "content": task},
        ]

        extra: dict = {}
        if config.OPUS_MODEL.startswith("openrouter/") and config.OPENROUTER_API_KEY:
            extra["api_key"] = config.OPENROUTER_API_KEY

        # Remove delegate_to_opus from tools to prevent recursion
        opus_tools = [t for t in TOOLS if t["function"]["name"] != "delegate_to_opus"]

        for _ in range(8):  # Opus gets more rounds
            response = await litellm.acompletion(
                model=config.OPUS_MODEL,
                messages=msgs,
                tools=opus_tools,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=4096,
                **extra,
            )
            choice = response.choices[0]
            msg = choice.message

            if choice.finish_reason == "tool_calls" and msg.tool_calls:
                msgs.append({"role": "assistant", "content": msg.content, "tool_calls": msg.tool_calls})
                for tc in msg.tool_calls:
                    try:
                        inputs = json.loads(tc.function.arguments)
                    except Exception:
                        inputs = {}
                    result = await self._execute_tool(tc.function.name, inputs)
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})
            else:
                return {"opus_result": msg.content or "Done."}

        # Force final response after max rounds
        response = await litellm.acompletion(
            model=config.OPUS_MODEL, messages=msgs, temperature=0.3, max_tokens=4096, **extra,
        )
        return {"opus_result": response.choices[0].message.content or "Done."}

    async def _execute_tool(self, name: str, inputs: dict) -> Any:
        try:
            if name == "get_state":
                return await self._ha.get_state(inputs["entity_id"])
            elif name == "search_statistics":
                return await self._ha.search_statistics(inputs.get("query", ""))
            elif name == "get_statistics":
                return await self._ha.get_statistics(
                    inputs["statistic_ids"],
                    inputs.get("period", "hour"),
                    inputs.get("hours", 48),
                )
            elif name == "get_states_by_domain":
                return await self._ha.get_entities_by_domain(inputs["domain"])
            elif name == "call_service":
                data = {"entity_id": inputs["entity_id"]}
                data.update(inputs.get("extra_data") or {})
                return await self._ha.call_service(inputs["domain"], inputs["service"], data)
            elif name == "get_history":
                return await self._ha.get_history(inputs["entity_id"], inputs.get("hours", 24))
            elif name == "delegate_to_opus":
                return await self._run_opus(inputs.get("task", ""))
            elif name == "search_entities":
                return _search_entities(inputs.get("query", ""))
            elif name == "add_custom_alert":
                return await _add_custom_alert(inputs)
            elif name == "remember":
                return _remember(inputs)
            elif name == "read_self":
                path = SELF_EDIT_FILES.get(inputs.get("filename", ""))
                if not path:
                    return {"error": "Unknown file"}
                return {"filename": inputs["filename"], "content": path.read_text() if path.exists() else ""}
            elif name == "write_self":
                path = SELF_EDIT_FILES.get(inputs.get("filename", ""))
                if not path:
                    return {"error": "Unknown file"}
                path.write_text(inputs["content"])
                return {"status": "written", "filename": inputs["filename"]}
            elif name == "read_ha_config":
                return _read_ha_config(inputs)
            elif name == "write_ha_config":
                return _write_ha_config(inputs)
            elif name == "reload_ha_config":
                return await self._ha.call_service(
                    inputs.get("component", "automation"), "reload", {}
                )
            else:
                return {"error": f"Unknown tool: {name}"}
        except Exception as e:
            logger.error(f"Tool {name} failed: {e}")
            return {"error": str(e)}


def _search_entities(query: str) -> dict:
    """Search ha_entities.md for lines matching a keyword."""
    if not ENTITIES_PATH.exists():
        return {"results": [], "note": "Entity reference file not found"}
    query_lower = query.lower()
    lines = ENTITIES_PATH.read_text().splitlines()
    matches = [line.strip() for line in lines if query_lower in line.lower() and line.strip()]
    if not matches:
        return {"results": [], "note": f"No entities matching '{query}'"}
    return {"results": matches[:20]}  # cap at 20 to limit tokens


def _remember(inputs: dict) -> dict:
    note = inputs.get("note", "").strip()
    if not note:
        return {"error": "No note provided"}
    existing = MEMORY_PATH.read_text() if MEMORY_PATH.exists() else ""
    MEMORY_PATH.write_text(existing + f"- {note}\n")
    return {"status": "remembered", "note": note}


def _read_ha_config(inputs: dict) -> dict:
    filename = inputs.get("filename", "")
    if filename not in ALLOWED_CONFIG_FILES:
        return {
            "error": f"Not allowed: {filename}. Permitted: {', '.join(sorted(ALLOWED_CONFIG_FILES))}"
        }
    path = Path("/homeassistant") / filename
    if not path.exists():
        return {"error": f"File not found: {filename}"}
    return {"filename": filename, "content": path.read_text()}


def _write_ha_config(inputs: dict) -> dict:
    filename = inputs.get("filename", "")
    if filename not in ALLOWED_CONFIG_FILES:
        return {"error": f"Not allowed: {filename}"}
    path = Path("/homeassistant") / filename
    backup = path.read_text() if path.exists() else ""
    path.write_text(inputs["content"])
    try:
        result = subprocess.run(
            ["ha", "core", "check"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            path.write_text(backup)
            return {
                "error": f"Validation failed: {(result.stdout + result.stderr).strip()}",
                "restored": True,
            }
        return {"status": "written", "filename": filename}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"status": "written", "filename": filename, "note": f"Could not validate: {e}"}


async def _add_custom_alert(inputs: dict) -> dict:
    import uuid
    alerts_path = Path(__file__).parent.parent / "user_alerts.json"
    alerts = []
    if alerts_path.exists():
        try:
            alerts = json.loads(alerts_path.read_text())
        except Exception:
            alerts = []

    new_alert = {
        "id": str(uuid.uuid4()),
        "entity_id": inputs["entity_id"],
        "condition": inputs["condition"],
        "threshold": inputs["threshold"],
        "message": inputs["message"],
        "enabled": True,
    }
    alerts.append(new_alert)
    alerts_path.write_text(json.dumps(alerts, indent=2))
    return {"status": "created", "alert": new_alert}
