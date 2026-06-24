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
    # Reuse the conversation voice layer so the briefing sounds like Jarvis,
    # then append the briefing-specific shape. (The old standalone briefing_prompt.md
    # never loaded the soul, which is why briefings read voiceless.)
    from jarvis.agents.conversation import _load_system_prompt as _voice_prompt
    base = _voice_prompt(mode="conversation")
    briefing_note = (
        "\n\n---\n\nBRIEFING MODE: Generate a morning briefing from current home state. "
        "Lead with the single most interesting or urgent thing. Under 150 words, plain prose, no markdown. "
        "Report what CHANGED since yesterday — do not re-list standing facts (water %, the same offline "
        "devices, the same backup time) that were already true in the previous briefing. Don't invent data. "
        "If an 'Anomalies detected' list is provided, lead with the notable ones and explain each in your "
        "own voice (what's unusual, by how much); if the list is empty or absent, don't mention anomalies. "
        "Every 'typical'/baseline figure in that list is THIS HOME'S OWN recent median, computed from its "
        "own history — it is NOT a regional, city, or national average. Never invent an external benchmark: "
        "do not say a figure is '174% of the NZ average', 'of the Auckland average', or similar unless that "
        "exact number was given to you. You MAY cite real comparison figures that ARE in the data — e.g. the "
        "Watercare sensor's own daily_average, or its household_efficiency_band (a genuine Watercare peer "
        "rating) — but otherwise describe a deviation as simply high or low versus this home's own recent "
        "usage. Do not invent a comparison you were not given. "
        "And don't re-headline the same standing deviation (e.g. water) morning after morning — if it has "
        "been flagged on recent days and hasn't materially changed, it's the new normal, not news; mention "
        "it briefly at most, or skip it."
    )
    return base + briefing_note


async def generate(ha_state_summary: str, anomalies: list[str] | None = None) -> str:
    from jarvis.router import complete

    from jarvis.config import config
    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc
    now = datetime.datetime.now(tz).strftime("%A %d %B %Y, %H:%M %Z")
    anomaly_block = ""
    if anomalies:
        anomaly_block = "\n\nAnomalies detected (vs typical):\n" + "\n".join(f"- {a}" for a in anomalies)
    user_msg = (
        f"Morning briefing request — {now}\n\n"
        f"Current home state:\n{ha_state_summary}"
        f"{anomaly_block}"
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
