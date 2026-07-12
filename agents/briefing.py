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
        "it briefly at most, or skip it. "
        "Do NOT ask whether the caravan will be used today, in any wording — a separate, fixed question "
        "about the caravan is appended after you generate this text, so asking yourself would duplicate "
        "it and the user would be asked twice. The 'Caravan auto-heat' note above describes how a REPLY "
        "to that follow-up question is handled later in conversation — it is not an instruction for you "
        "to raise the topic here. You may still mention the caravan's temperature as a plain fact if it's "
        "notable, just never as a question about today's plans."
    )
    return base + briefing_note


async def fetch_water_context(ha_client) -> str | None:
    """A compact, factual water block from the Watercare sensor's real attributes — this
    home's OWN daily_average and the Watercare household_efficiency_band — so the briefing
    can speak about water from real figures instead of confabulating a regional average.
    Returns None if the sensor is missing/unavailable or carries no useful figures."""
    from jarvis.config import config
    try:
        st = await ha_client.get_state(config.WATERCARE_SENSOR)
    except Exception:
        return None
    if not st:
        return None
    if str(st.get("state")).strip().lower() in ("unavailable", "unknown", "none", ""):
        return None
    a = st.get("attributes") or {}
    parts: list[str] = []
    avg = a.get("daily_average", a.get("currentPeriodAverage"))
    if avg is not None:
        parts.append(f"this home's own daily average ~{avg} L")
    band = a.get("household_efficiency_band", a.get("currentHouseholdBand"))
    if band is not None:
        parts.append(f"Watercare efficiency band {band} (their peer rating, not an average)")
    cost = a.get("current_period_cost")
    if cost is not None:
        parts.append(f"cost so far ${cost} {a.get('cost_currency', 'NZD')}")
    pf, pt = a.get("billing_period_from"), a.get("billing_period_to")
    if pf and pt:
        parts.append(f"billing period {str(pf)[:10]} to {str(pt)[:10]}")
    if not parts:
        return None
    return (
        "Watercare (this home's OWN smart-meter data — NOT a regional/NZ/Auckland average): "
        + "; ".join(parts) + "."
    )


async def generate(
    ha_state_summary: str,
    anomalies: list[str] | None = None,
    water_context: str | None = None,
) -> str:
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
    water_block = f"\n\n{water_context}" if water_context else ""
    user_msg = (
        f"Morning briefing request — {now}\n\n"
        f"Current home state:\n{ha_state_summary}"
        f"{anomaly_block}"
        f"{water_block}"
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
