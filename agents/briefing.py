from __future__ import annotations
import logging
import datetime
import zoneinfo
from pathlib import Path

logger = logging.getLogger(__name__)

BRIEFING_PROMPT_PATH = Path(__file__).parent.parent / "briefing_prompt.md"

def _fallback_prompt() -> str:
    from jarvis.config import config
    return (
        f"You are {config.BOT_NAME}, the AI for a smart home. "
        "Generate a morning briefing based on current home state. Under 150 words. "
        "Plain prose only. Lead with the most interesting thing. Don't invent data."
    )


def _load_system_prompt() -> str:
    if BRIEFING_PROMPT_PATH.exists():
        return BRIEFING_PROMPT_PATH.read_text().strip()
    return _fallback_prompt()


async def generate(ha_state_summary: str) -> str:
    from jarvis.router import complete

    from jarvis.config import config
    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc
    now = datetime.datetime.now(tz).strftime("%A %d %B %Y, %H:%M %Z")
    user_msg = (
        f"Morning briefing request — {now}\n\n"
        f"Current home state:\n{ha_state_summary}"
    )

    try:
        return await complete(
            "briefing",
            [
                {"role": "system", "content": _load_system_prompt()},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=400,
            temperature=0.5,
        )
    except Exception as e:
        logger.error(f"Briefing agent failed: {e}")
        return f"Good morning. (Briefing unavailable: {e})"
