from __future__ import annotations
import asyncio
import json
import logging
import os
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from typing import Any
import litellm
from jarvis.router import build_cached_messages
from jarvis.usage import log_completion

logger = logging.getLogger(__name__)


def _tz() -> str:
    from jarvis.config import config
    return config.TIMEZONE


def _bot_name() -> str:
    from jarvis.config import config
    return config.BOT_NAME


def _owner_name() -> str:
    from jarvis.config import config
    return config.OWNER_NAME

MAX_HISTORY = 20
MAX_TOOL_ROUNDS = 5
MAX_PROACTIVE_TOOL_ROUNDS = 2

SOUL_PATH = Path(__file__).parent.parent / "soul.md"
ENTITIES_PATH = Path(__file__).parent.parent / "ha_entities.md"
MEMORY_PATH = Path(__file__).parent.parent / "memory.md"
BRIEFING_PROMPT_PATH = Path(__file__).parent.parent / "briefing_prompt.md"

SELF_EDIT_FILES = {
    "soul.md": SOUL_PATH,
    "ha_entities.md": ENTITIES_PATH,
    "briefing_prompt.md": BRIEFING_PROMPT_PATH,
    "memory.md": MEMORY_PATH,
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
            "name": "send_message",
            "description": (
                "Send a Telegram message to the user immediately, without waiting for the end of your turn. "
                "Use for: progress updates mid-task ('On it, querying energy stats now...'), "
                "delivering an answer when triggered by a HA event, or follow-up messages. "
                "If you use send_message to deliver the full answer, return an empty string as your final response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message to send to the user"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Send a question to the user via Telegram and wait for their reply before continuing. "
                "Use when you need confirmation before taking an action "
                "(e.g. 'The garage heater is set to frost protection — still turn it off?'). "
                "Returns the user's reply as a string. "
                "Do not use in proactive mode ([PROACTIVE] messages) — use send_message instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Question to ask the user"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Seconds to wait for reply (default 120)",
                    },
                },
                "required": ["prompt"],
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
            "name": "set_mode",
            "description": (
                "Set Jarvis's operating mode when the user asks (e.g. 'go quiet', 'away mode', "
                "'switch to storm mode', 'back to normal/standard'). Modes change proactivity: "
                "quiet = silent except genuine safety; standard = normal; away = security-vigilant "
                "for when nobody's home; storm = weather-vigilant. Writes the mode to Home Assistant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["quiet", "standard", "away", "storm"],
                    }
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_caravan_heating",
            "description": (
                "Enable or disable caravan heating: the master toggle, the auto-heat automation, "
                "and any heartbeats (the entities in CARAVAN_ENTITIES). Call with enabled=true when "
                "the user says they plan to use the caravan that day (typically in reply to the "
                "morning briefing's caravan question), and enabled=false when they won't be — or "
                "when they say they're done/finished in the caravan. Disabling also switches the "
                "physical heater plugs off immediately, so any running heaters actually stop. "
                "Enabling fires the auto-heat now and arms a power-draw check that self-heals and "
                "alerts if the caravan isn't actually heating a few minutes later."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "enabled": {
                        "type": "boolean",
                        "description": "true to turn the caravan heating automations on, false to turn them off",
                    },
                    "trigger_now": {
                        "type": "boolean",
                        "description": "Run the auto-heat automation immediately after enabling (default true; you normally never need to set this)",
                    },
                },
                "required": ["enabled"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_anomalies",
            "description": (
                "Check today's home metrics (water, energy, power, spa, ...) against the learned "
                "baseline and return any anomalies. This is the same engine the morning briefing "
                "uses, but on demand. Use when the user asks if anything is unusual / off / out of "
                "the ordinary, or 'any anomalies?'."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_changes",
            "description": (
                "See what was recently changed — in your own code (scope 'jarvis') or in the Home "
                "Assistant configuration (scope 'config'). Uses git history when available, "
                "otherwise the most recently modified files. Use when the user asks what's new, "
                "what changed, or what you've been working on lately."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["jarvis", "config"],
                        "description": "'jarvis' = your own codebase; 'config' = the HA config dir. Default 'jarvis'.",
                    },
                    "limit": {"type": "integer", "description": "Max entries to return (default 15)."},
                },
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
                "Available: soul.md (personality), ha_entities.md (known entities), "
                "briefing_prompt.md (morning briefing instructions), memory.md (persistent memory and notification preferences)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "enum": ["soul.md", "ha_entities.md", "briefing_prompt.md", "memory.md"],
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
                "Use this to update or remove memory entries — read_self first, edit the content, write it back. "
                "Use 'remember' to append a new note; use write_self to update or remove an existing one. "
                "Changes take effect on the next message. "
                "Available: soul.md, ha_entities.md, briefing_prompt.md, memory.md."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "enum": ["soul.md", "ha_entities.md", "briefing_prompt.md", "memory.md"],
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


async def _current_mode_line(ha_client: Any) -> str:
    """Resolve the active operating mode and return a one-line posture banner for the
    per-call user message (kept out of the cached system prompt because the mode is
    volatile). Returns '' on any failure — mode awareness must never block a reply."""
    try:
        from jarvis.scheduler import resolve_mode, MODES
        mode = await resolve_mode(ha_client)
        posture = MODES.get(mode, {}).get("posture", "")
        return f"(mode: {mode} — {posture})" if posture else f"(mode: {mode})"
    except Exception:
        return ""


def _operational_layer() -> str:
    """Tools/timezone/formatting rules. Deliberately NO terseness mandate (it buried the
    voice) and NO current-time string (kept out of the static prefix so it can be cached;
    the clock is injected into the per-call user message instead)."""
    return (
        "You have tools to read entity states, control devices, remember things, and edit HA config files.\n"
        "To find entity IDs: use search_entities with a broad keyword. "
        "If search_entities returns nothing, try a different keyword, then try get_states_by_domain, then try get_state with a guessed ID. "
        "Never give up after one failed search — try at least 3 approaches.\n"
        "When taking actions, confirm what you did in one sentence.\n"
        "When asked questions, fetch live data — never guess entity IDs without trying.\n\n"
        f"TIMEZONE: All HA entity timestamps (last_changed, last_updated, etc.) are UTC. "
        f"Local timezone is {_tz()}. Always convert UTC to local time before reporting any time or date. "
        f"Never report a UTC timestamp as if it were local time — a HA timestamp of 21:00 UTC is not 9pm locally.\n\n"
        "FORMATTING: Never use markdown. No bold, italics, tables, * bullets, # headers, backticks.\n\n"
        "Never say 'certainly', 'of course', 'happy to help', 'great question'. Don't pad — but do not flatten "
        "your voice into a terse status report either. Speak as yourself.\n\n"
        "CAPABILITIES:\n"
        "Operating modes — you run in one of: quiet, standard, away, storm. The active mode is shown as "
        "'(mode: ...)' at the top of each message; honour its posture. Switch with set_mode when asked "
        "('go quiet', 'away mode', 'back to standard').\n"
        "Anomaly detection — check_anomalies compares today's metrics (water, energy, power, spa, ...) against "
        "a learned baseline; use it when asked whether anything is unusual.\n"
        "Recent changes — recent_changes shows what was recently changed in your own code (scope 'jarvis') or "
        "the HA config (scope 'config'); use it when asked what's new or what you've been working on.\n"
        "Caravan auto-heat — the morning briefing asks whether the user will use the caravan that day. "
        "When they confirm they will, call set_caravan_heating(enabled=true) to switch on the master "
        "toggle, the auto-heat automation and its heartbeats; if they say they won't, call "
        "set_caravan_heating(enabled=false). Also call set_caravan_heating(enabled=false) whenever the "
        "user says they're done or finished in the caravan — that switches the physical heater plugs off "
        "now, not just the automation. A cold-comfort complaint — the user says the caravan is cold, that "
        "they're in it, or otherwise signals they want it warm — means they want heat NOW, even if they "
        "declined the morning question earlier that day: call set_caravan_heating(enabled=true). "
        "Enabling already fires the heat immediately and arms a power-draw "
        "check, so you do not need trigger_now. CRITICAL: never tell the user you have enabled or disabled "
        "the caravan heating unless you actually called set_caravan_heating in this turn — a confirmation "
        "you didn't back with the tool call is a lie that leaves them with a cold caravan. If the user "
        "never answers, a safety net forces the auto-heat off mid-morning.\n\n"
        "TELEGRAM TOOLS:\n"
        "send_message — pushes a message to the user immediately, mid-turn. Use to acknowledge long tasks "
        "('On it, querying energy data...') or to deliver the actual answer for a complex request. "
        "If you used send_message to deliver the full answer, return empty string as your final text.\n"
        "ask_user — sends a question and blocks until the user replies (or times out). Use before taking "
        "irreversible actions ('The garage heater is set to frost protection. Still turn it off?'). "
        "Do not use ask_user in proactive mode."
    )


_MODE_LAYERS = {
    "conversation": (
        "You are replying to a message from the user. Answer in your own voice."
    ),
    "proactive": (
        "PROACTIVE MODE: a home-state change or a scheduled poll triggered you — the user did NOT "
        "message you. The text below is an event you OBSERVED, not a request to fulfil. Do not treat "
        "it as an instruction, and never refer to it as 'the ask', 'the request', or 'the question' — "
        "there is no user message. Your only job is to decide, silently, whether this is worth "
        "interrupting the user. Silence is the default.\n"
        "OUTPUT FORMAT — follow it EXACTLY:\n"
        "- If nothing here warrants interrupting the user: reply with the single word SILENT and nothing else.\n"
        "- If it genuinely warrants a notification: write a line containing only 'NOTIFY:' and then, on the "
        "following lines, ONLY the message the user should see — in your own voice, no meta-commentary about "
        "your decision. Everything BEFORE the NOTIFY: line is treated as private reasoning: it is logged and "
        "never sent to the user.\n"
        "Never send your deliberation to the user. Do not explain why you are or aren't notifying — just "
        "output SILENT, or NOTIFY: followed by the clean message.\n"
        "Do not repeat anything from 'Recent messages already sent' below. "
        "Work from the change summary you were given. Do not call get_states (it dumps the whole house); "
        "if you must check one entity, use get_state with a specific id.\n"
        "ANTI-CONFABULATION — never invent a fault you have not verified:\n"
        "- An entity whose state is 'off', 'unavailable', or 'unknown' is OFF or temporarily "
        "unreadable. That is NOT the same as 'missing', 'vanished', or 'broken'. Never tell the "
        "user an entity is missing or an automation is broken unless a get_state call you made "
        "THIS turn returned an error or an explicit not-found for that exact entity id.\n"
        "- Never mention 'repair issues' or claim repairs are active unless a tool you called this "
        "turn actually returned them. Do not infer repairs from a state change.\n"
        "- A falling temperature on something whose heater/heating is OFF is the EXPECTED result of "
        "it being off, not a fault — stay SILENT. Heating being off because the user finished with a "
        "space (e.g. said they were done in the caravan) is normal and not notify-worthy.\n"
        "- If you are unsure whether something is genuinely wrong, the answer is SILENT. Do not "
        "escalate, and never re-send a variant of an alert you already sent."
    ),
}


def _load_system_prompt(mode: str = "conversation") -> str:
    """Assemble voice + operational + mode layers. The result is static across calls so it can be
    prompt-cached; volatile data (current time, recent messages) is injected via the user message."""
    op = _operational_layer()
    mode_layer = _MODE_LAYERS.get(mode, _MODE_LAYERS["conversation"])

    memory = ""
    if MEMORY_PATH.exists():
        mem = MEMORY_PATH.read_text().strip()
        if mem:
            memory = f"\n\nYour persistent memory notes:\n{mem}"

    if SOUL_PATH.exists():
        from jarvis.config import config
        soul = SOUL_PATH.read_text()
        soul = soul.replace("{BOT_NAME}", config.BOT_NAME)
        soul = soul.replace("{OWNER_NAME}", config.OWNER_NAME)
        voice = soul
    else:
        voice = (
            f"You are {_bot_name()}, an AI smart home assistant. "
            f"The person you are talking to is named {_owner_name()}."
        )

    return f"{voice}\n\n---\n\n{op}\n\n---\n\n{mode_layer}{memory}"


_HUMAN_SERVICE = {"turn_on": "on", "turn_off": "off", "toggle": "toggled"}


def _check_silent(content: str) -> tuple[bool, str]:
    """Return (is_silent, reasoning). SILENT may appear anywhere as a standalone line."""
    lines = content.strip().split('\n')
    non_empty = [l.strip() for l in lines if l.strip()]
    if any(l.upper() == "SILENT" for l in non_empty):
        reasoning = '\n'.join(l for l in non_empty if l.upper() != "SILENT").strip()
        return True, reasoning
    return False, ""


def _split_notify(content: str) -> tuple[str, str]:
    """Split a proactive reply around a 'NOTIFY:' marker into (message_to_send, reasoning).
    Everything before the marker is private deliberation (logged, never sent); everything
    from the marker onward is the user-facing message. With no marker, the whole text is the
    message (back-compat) and there is no separate reasoning."""
    lines = content.strip().split('\n')
    for i, line in enumerate(lines):
        if line.strip().upper().startswith("NOTIFY:"):
            after = line.strip()[len("NOTIFY:"):].strip()
            tail = [after] if after else []
            msg = '\n'.join(tail + lines[i + 1:]).strip()
            reasoning = '\n'.join(lines[:i]).strip()
            return msg, reasoning
    return content.strip(), ""


def _finalize_proactive(content: str) -> str | None:
    """Reduce a raw proactive completion to just what the user should see, or None to stay
    silent. Strips leaked reasoning on BOTH paths: SILENT (no notification) and NOTIFY:
    (notification with the deliberation stripped). An empty message is treated as silent."""
    is_silent, reasoning = _check_silent(content)
    if is_silent:
        if reasoning:
            logger.info(f"Proactive SILENT reasoning: {reasoning}")
        return None
    msg, reasoning = _split_notify(content)
    if reasoning:
        logger.info(f"Proactive reasoning (not sent): {reasoning}")
    return msg or None


def _format_tool_footer(tool_log: list[tuple[str, dict]]) -> str:
    """Compact footer showing what the bot actually did. No raw entity IDs."""
    reads = 0
    actions: list[str] = []

    for name, inputs in tool_log:
        if name in ("get_state", "get_states_by_domain", "get_history", "get_statistics", "search_statistics", "read_ha_config", "read_self", "search_entities", "check_anomalies", "recent_changes"):
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
        elif name == "set_mode":
            actions.append(f"set mode {inputs.get('mode', '?')}")
        elif name == "set_caravan_heating":
            actions.append("caravan heating " + ("on" if inputs.get("enabled") else "off"))
        elif name == "add_custom_alert":
            actions.append("added alert")
        elif name == "send_message":
            actions.append("sent message")
        elif name == "ask_user":
            actions.append("asked user")

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
        self._recent_alerts: deque[str] = deque(maxlen=8)
        self._caravan_verify_task: asyncio.Task | None = None

    def _record_sent(self, text: str) -> None:
        """Remember a full (untruncated) message we just sent, for dedup on later polls."""
        self._recent_alerts.append(text.strip())

    def note_briefing(self, chat_id: int, text: str) -> None:
        """Record a proactively-sent briefing into conversation history so the user's later
        reply (e.g. answering the caravan question) has context. Mirrors the user/assistant
        turn shape used elsewhere so the message list stays valid (no assistant-first turn)."""
        history = self._history[chat_id]
        history.append({"role": "user", "content": f"(now: {_now_str()}) [SCHEDULED BRIEFING]"})
        history.append({"role": "assistant", "content": text})
        if len(history) > MAX_HISTORY:
            history[:] = history[-MAX_HISTORY:]

    async def reply(self, chat_id: int, user_text: str) -> str:
        self._agent_busy = True
        history = self._history[chat_id]
        prefix = f"(now: {_now_str()})"
        mode_line = await _current_mode_line(self._ha)
        if mode_line:
            prefix += f"\n{mode_line}"
        history.append({"role": "user", "content": f"{prefix}\n{user_text}"})

        if len(history) > MAX_HISTORY:
            history[:] = history[-MAX_HISTORY:]

        try:
            response_text = await self._run_with_tools(history)
            history.append({"role": "assistant", "content": response_text})
            return response_text
        except Exception as e:
            logger.error(f"Conversation agent failed: {e}")
            return f"Error: {e}"
        finally:
            self._agent_busy = False

    async def run_proactive(self, context: str, chat_id: int, use_history: bool = True, model: str | None = None) -> None:
        """
        Run agent from a HA event or scheduler trigger (not a user message).

        use_history=True  (default) — for HA events. Adds [PROACTIVE] message to
            shared conversation history so the user can follow up.

        use_history=False — for periodic heartbeat polls. Runs with a throwaway
            scratch context so polling never floods the conversation history.

        Agent uses send_message for output; final text is also sent unless it is
        empty or the literal 'SILENT'.
        """
        if self._agent_busy:
            logger.warning("run_proactive skipped — agent busy")
            return
        self._agent_busy = True

        if use_history:
            messages = self._history[chat_id]
            messages.append({"role": "user", "content": f"(now: {_now_str()}) [PROACTIVE] {context}"})
            if len(messages) > MAX_HISTORY:
                messages[:] = messages[-MAX_HISTORY:]
        else:
            # Throwaway context — don't touch shared history at all
            messages = [{"role": "user", "content": f"(now: {_now_str()}) [PROACTIVE] {context}"}]

        try:
            response_text = await self._run_with_tools(messages, model=model, mode="proactive")
            if use_history:
                self._history[chat_id].append({"role": "assistant", "content": response_text})
            stripped = response_text.strip()
            if stripped and stripped.upper() != "SILENT" and self._send_fn:
                self._record_sent(stripped)
                await self._send_fn(stripped)
        except Exception as e:
            logger.error(f"run_proactive failed: {e}")
        finally:
            self._agent_busy = False

    async def _run_with_tools(self, messages: list[dict], model: str | None = None, mode: str = "conversation") -> str:
        active_model = model or self._model
        # Reload system prompt each call so memory/entity changes are live
        msgs = [{"role": "system", "content": _load_system_prompt(mode)}] + messages
        tool_log: list[tuple[str, dict]] = []
        rounds = 0

        from jarvis.config import config
        extra: dict = {}
        if active_model.startswith("openrouter/") and config.OPENROUTER_API_KEY:
            extra["api_key"] = config.OPENROUTER_API_KEY
        elif active_model.startswith("anthropic/") and config.ANTHROPIC_API_KEY:
            extra["api_key"] = config.ANTHROPIC_API_KEY

        round_cap = MAX_PROACTIVE_TOOL_ROUNDS if mode == "proactive" else MAX_TOOL_ROUNDS
        while rounds < round_cap:
            response = await litellm.acompletion(
                model=active_model,
                messages=build_cached_messages(msgs, active_model),
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.5,
                max_tokens=1024,
                **extra,
            )
            log_completion(response, "conversation")

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
                        model=active_model, messages=build_cached_messages(msgs, active_model), temperature=0.5, max_tokens=1024, **extra,
                    )
                    log_completion(retry, "conversation")
                    content = retry.choices[0].message.content or "I checked but couldn't formulate a response."
                # In proactive mode, strip leaked deliberation: the user only ever sees a
                # clean NOTIFY: message, never the model's reasoning about whether to speak.
                if mode == "proactive":
                    send_text = _finalize_proactive(content)
                    if send_text is None:
                        return "SILENT"
                    return send_text + _format_tool_footer(tool_log)
                return content + _format_tool_footer(tool_log)

        # Hit max tool rounds — force a final response without tools
        msgs.append({"role": "user", "content": "Based on everything you found, give your answer now."})
        response = await litellm.acompletion(
            model=active_model, messages=build_cached_messages(msgs, active_model), temperature=0.5, max_tokens=1024, **extra,
        )
        log_completion(response, "conversation")
        content = response.choices[0].message.content or "I checked but couldn't formulate a response."
        if mode == "proactive":
            send_text = _finalize_proactive(content)
            if send_text is None:
                return "SILENT"
            return send_text + _format_tool_footer(tool_log)
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
            f"TIMEZONE: All HA timestamps are UTC. Local timezone is {_tz()}. Convert all times.\n"
            "FORMATTING: Plain text only. No markdown."
        )
        msgs = [
            {"role": "system", "content": opus_system},
            {"role": "user", "content": f"(now: {_now_str()}) {task}"},
        ]

        extra: dict = {}
        if config.OPUS_MODEL.startswith("openrouter/") and config.OPENROUTER_API_KEY:
            extra["api_key"] = config.OPENROUTER_API_KEY
        elif config.OPUS_MODEL.startswith("anthropic/") and config.ANTHROPIC_API_KEY:
            extra["api_key"] = config.ANTHROPIC_API_KEY

        # Remove delegate_to_opus from tools to prevent recursion
        opus_tools = [
            t for t in TOOLS
            if t["function"]["name"] not in ("delegate_to_opus", "send_message", "ask_user")
        ]

        for _ in range(8):  # Opus gets more rounds
            response = await litellm.acompletion(
                model=config.OPUS_MODEL,
                messages=build_cached_messages(msgs, config.OPUS_MODEL),
                tools=opus_tools,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=4096,
                **extra,
            )
            log_completion(response, "opus")
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
            model=config.OPUS_MODEL, messages=build_cached_messages(msgs, config.OPUS_MODEL), temperature=0.3, max_tokens=4096, **extra,
        )
        log_completion(response, "opus")
        return {"opus_result": response.choices[0].message.content or "Done."}

    async def _set_caravan_heating(self, enabled: bool, trigger_now: bool = True) -> dict:
        """Turn the configured caravan entities (master toggle + auto-heat automation +
        heartbeats) on or off. Records the decision so the 09:00 safety net won't override
        it. When enabling, fires the auto-heat now (trigger_now defaults True) and arms a
        delayed power-draw verification that self-heals if the caravan isn't actually
        heating — so a silent failure can't leave the user with a cold caravan."""
        from jarvis import caravan
        # Cancel any in-flight verification from a prior enable — a fresh decision (or a
        # disable) supersedes it, and a stale check could re-enable against the user's wish.
        if self._caravan_verify_task and not self._caravan_verify_task.done():
            self._caravan_verify_task.cancel()
        self._caravan_verify_task = None
        result = await caravan.set_caravan(self._ha, enabled, trigger_now)
        caravan.mark_decided()
        if enabled and self._send_fn:
            self._caravan_verify_task = asyncio.create_task(self._verify_caravan_draw_later())
        return result

    async def _verify_caravan_draw_later(self) -> None:
        """Wait CARAVAN_VERIFY_DELAY_MIN, then confirm the caravan is actually drawing
        power; self-heal and notify if not. Best-effort — never raises into the loop."""
        from jarvis import caravan
        from jarvis.config import config
        try:
            await asyncio.sleep(max(0.0, config.CARAVAN_VERIFY_DELAY_MIN) * 60)
            await caravan.verify_drawing(self._ha, self._send_fn)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"caravan power-draw verification failed: {e}")

    async def _ask_user_impl(self, prompt: str, timeout: int) -> dict:
        """Send prompt to user and block until they reply or timeout."""
        if not self._send_fn:
            return {"error": "No send function configured"}
        await self._send_fn(prompt)
        loop = asyncio.get_event_loop()
        self._pending_reply = loop.create_future()
        try:
            reply = await asyncio.wait_for(self._pending_reply, timeout=timeout)
            return {"reply": reply}
        except asyncio.TimeoutError:
            return {"reply": "[no reply — timed out]"}
        finally:
            self._pending_reply = None

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
            elif name == "set_mode":
                import os as _os
                mode = str(inputs.get("mode", "")).strip().lower()
                if mode not in ("quiet", "standard", "away", "storm"):
                    return {"error": f"Unknown mode '{mode}'"}
                entity = _os.environ.get("MODE_ENTITY", "input_select.jarvis_mode")
                await self._ha.call_service(
                    "input_select", "select_option", {"entity_id": entity, "option": mode}
                )
                return {"status": "ok", "mode": mode}
            elif name == "set_caravan_heating":
                return await self._set_caravan_heating(
                    bool(inputs.get("enabled")), bool(inputs.get("trigger_now", True))
                )
            elif name == "check_anomalies":
                from jarvis.anomaly import detect_and_surface
                anomalies = await detect_and_surface(self._ha)
                if anomalies:
                    return {"anomalies": anomalies}
                return {"anomalies": [], "note": "Nothing unusual vs the learned baseline."}
            elif name == "recent_changes":
                return _recent_changes(inputs.get("scope", "jarvis"), inputs.get("limit", 15))
            elif name == "search_entities":
                return _search_entities(inputs.get("query", ""))
            elif name == "add_custom_alert":
                return await _add_custom_alert(inputs)
            elif name == "send_message":
                if self._send_fn:
                    await self._send_fn(inputs.get("text", ""))
                    return {"status": "sent"}
                return {"error": "No send function configured"}
            elif name == "ask_user":
                return await self._ask_user_impl(
                    inputs.get("prompt", ""), inputs.get("timeout_seconds", 120)
                )
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


def _change_root(scope: str) -> Path:
    """Map a recent_changes scope to a directory. 'jarvis' = the deployed code dir
    (/config/jarvis in the add-on); 'config' = the HA config dir one level up (/config)."""
    root = Path(__file__).parent.parent
    return root.parent if scope == "config" else root


def _collect_changes(root: Path, limit: int) -> dict:
    """Recent changes under `root`: git log if it's a repo and git is installed, else the
    most recently modified files by mtime. The mtime fallback means this works even when
    the dir was deployed via scp (no .git) or git is absent from the container."""
    if not root.exists():
        return {"error": f"Path not found: {root}"}
    try:
        # `-- .` scopes the log to `root` itself, so when jarvis lives inside the HA
        # config git repo (the Git add-on tracks /config), scope='jarvis' shows only
        # commits touching /config/jarvis rather than the whole config history.
        result = subprocess.run(
            ["git", "-C", str(root), "log", "--pretty=format:%h %ad %s", "--date=short", "-n", str(limit), "--", "."],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"source": "git", "root": str(root), "commits": result.stdout.strip().splitlines()}
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass  # git not installed or not a repo — fall through to mtime

    import datetime
    skip_dirs = {".git", "__pycache__", ".pytest_cache", "node_modules"}
    entries: list[tuple[float, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn.endswith(".pyc"):
                continue
            fp = Path(dirpath) / fn
            try:
                entries.append((fp.stat().st_mtime, str(fp.relative_to(root))))
            except OSError:
                continue
    entries.sort(reverse=True)
    recent = [
        {"file": rel, "modified": datetime.datetime.fromtimestamp(mt).strftime("%Y-%m-%d %H:%M")}
        for mt, rel in entries[:limit]
    ]
    return {"source": "mtime", "root": str(root), "recent_files": recent}


def _recent_changes(scope: str, limit: int) -> dict:
    scope = (scope or "jarvis").strip().lower()
    if scope not in ("jarvis", "config"):
        scope = "jarvis"
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 15
    limit = max(1, min(limit, 50))
    return _collect_changes(_change_root(scope), limit)


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
