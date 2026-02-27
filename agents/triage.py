from __future__ import annotations
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"ignore", "log", "notify", "needs_input"}

SYSTEM_PROMPT = """You are Jarvis, an AI home automation assistant for a house in Auckland, NZ.
Your job is to classify incoming Home Assistant events and decide the appropriate action.

Respond with EXACTLY one word — no explanation:
- "notify"      — user needs to know immediately (security, urgent, unexpected)
- "needs_input" — requires user decision (e.g. "spa has been on 6 hours, intentional?")
- "log"         — worth recording but not urgent
- "ignore"      — routine, expected, or low importance

Context to consider: time of day, whether the event is security-related, whether it's expected behaviour.
Security events (door, moisture, lock) at unusual hours → notify.
Routine power toggles or expected climate adjustments → log or ignore.
Anything requiring a yes/no decision → needs_input."""


async def classify(event: dict, ha_context: str) -> str:
    from jarvis.router import complete

    now = datetime.now().strftime("%A %H:%M")
    user_msg = (
        f"Time: {now}\n"
        f"Event title: {event.get('title', '')}\n"
        f"Event message: {event.get('message', '')}\n"
        f"Entity: {event.get('entity_id', '')}\n\n"
        f"Relevant home state:\n{ha_context}"
    )

    try:
        raw = await complete(
            "triage",
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=10,
            temperature=0.0,
        )
        action = raw.strip().lower().split()[0] if raw.strip() else "notify"
        return action if action in VALID_ACTIONS else "notify"
    except Exception as e:
        logger.error(f"Triage agent failed: {e} — defaulting to notify")
        return "notify"
