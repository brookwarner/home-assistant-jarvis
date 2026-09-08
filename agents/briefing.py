from __future__ import annotations
import asyncio
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
        "Do not spend words narrating things that are working. 'That's correct', 'idling normally', "
        "'everything else is quiet' — one short clause covers all of that, not a paragraph. "
        "Silence about a system means it's fine; only explain a system's behaviour when it did "
        "something unexpected, or when the explanation changes what the user would do today. "
        "If a 'Today's calendar' line is provided, use it to frame the day rather than reciting it: "
        "connect commitments to the house. Out all day means the spa and heating can idle; an evening "
        "out plus rain due at 3pm means the washing needs bringing in first; an early start means "
        "preconditioning has to happen sooner. Presence (person.* entities — home, not_home, or a "
        "zone name) is state, not gossip: use it the same way, and never speculate about where "
        "someone is beyond what the entity says. If no calendar line is given, say nothing about "
        "the calendar at all — do not announce an empty day. "
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
    if not _is_live(st.get("state")):
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


def _is_live(state: object) -> bool:
    """Whether an entity state carries a real reading rather than a dead/absent one."""
    return str(state).strip().lower() not in ("unavailable", "unknown", "none", "")


def _day_bounds(now: datetime.datetime) -> tuple[str, str]:
    """Local midnight to next local midnight, as UTC ISO strings for the HA calendar API."""
    start_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + datetime.timedelta(days=1)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return (
        start_local.astimezone(datetime.timezone.utc).strftime(fmt),
        end_local.astimezone(datetime.timezone.utc).strftime(fmt),
    )


def _format_event(event: dict, tz: datetime.tzinfo) -> tuple[bool, str] | None:
    """Render one event as (is_all_day, text): '09:30-10:30 Dentist' for a timed event,
    'Recycling collection (all day)' for an all-day one.

    is_all_day is returned alongside rather than re-derived from the text, so ordering keys off
    the event's own data — an event *titled* '... (all day)' must not be mistaken for one.

    A timed event whose timestamps won't parse degrades to the bare summary. It is NOT
    relabelled all-day: that would assert something false, which the model then repeats."""
    summary = (event.get("summary") or "").strip()
    if not summary:
        return None
    start, end = event.get("start") or {}, event.get("end") or {}
    if "dateTime" not in start:
        return (True, f"{summary} (all day)")
    try:
        s = datetime.datetime.fromisoformat(start["dateTime"]).astimezone(tz)
        label = s.strftime("%H:%M")
        if "dateTime" in end:
            e = datetime.datetime.fromisoformat(end["dateTime"]).astimezone(tz)
            label = f"{label}-{e.strftime('%H:%M')}"
    except (ValueError, TypeError):
        return (False, summary)
    return (False, f"{label} {summary}")


def _event_key(event: dict) -> tuple[str, str]:
    """Identity for dedup. The same Google event is often synced into several calendar
    entities; listing it twice reads as two separate commitments."""
    start = event.get("start") or {}
    return (
        (event.get("summary") or "").strip().lower(),
        str(start.get("dateTime") or start.get("date") or ""),
    )


async def fetch_calendar_context(ha_client, states: list[dict]) -> str | None:
    """Today's events across this home's live calendars, as a compact block.

    Calendar entities are filtered on their state first: ~50 of this home's ~75 are stale
    duplicates from a re-added integration and sit at 'unavailable', where the events endpoint
    returns HTTP 400. A per-calendar failure is skipped, never fatal. Returns None when there
    is nothing on — a quiet day should add no prompt text at all, rather than an empty header
    the model might narrate."""
    from jarvis.config import config

    try:
        tz = zoneinfo.ZoneInfo(config.TIMEZONE)
    except Exception:
        tz = datetime.timezone.utc

    allowlist = set(config.BRIEFING_CALENDARS or ())
    excluded = set(config.BRIEFING_CALENDARS_EXCLUDE or ())
    entity_ids = [
        s["entity_id"]
        for s in states
        if s.get("entity_id", "").startswith("calendar.")
        and _is_live(s.get("state"))
        and (not allowlist or s["entity_id"] in allowlist)
        and s["entity_id"] not in excluded
    ]
    if not entity_ids:
        return None

    start, end = _day_bounds(datetime.datetime.now(tz))
    results = await asyncio.gather(
        *(ha_client.get_calendar_events(e, start, end) for e in entity_ids),
        return_exceptions=True,
    )

    seen: set[tuple[str, str]] = set()
    rendered: list[tuple[bool, str]] = []
    for entity_id, result in zip(entity_ids, results):
        if isinstance(result, BaseException):
            # gather() hands cancellation back as a result; swallowing it would leave the
            # briefing task un-cancellable at shutdown.
            if isinstance(result, asyncio.CancelledError):
                raise result
            logger.debug(f"calendar {entity_id} unreadable: {result}")
            continue
        for event in result or []:
            key = _event_key(event)
            if key in seen:
                continue
            seen.add(key)
            item = _format_event(event, tz)
            if item:
                rendered.append(item)

    if not rendered:
        return None
    # All-day items last so timed commitments lead.
    rendered.sort()
    lines = [text for _, text in rendered]
    capped = lines[: config.BRIEFING_CALENDAR_MAX_EVENTS]
    dropped = len(lines) - len(capped)
    block = "Today's calendar: " + "; ".join(capped) + "."
    if dropped:
        block += f" (+{dropped} more not shown.)"
    return block


async def generate(
    ha_state_summary: str,
    anomalies: list[str] | None = None,
    water_context: str | None = None,
    calendar_context: str | None = None,
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
    calendar_block = f"\n\n{calendar_context}" if calendar_context else ""
    user_msg = (
        f"Morning briefing request — {now}\n\n"
        f"Current home state:\n{ha_state_summary}"
        f"{anomaly_block}"
        f"{water_block}"
        f"{calendar_block}"
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
