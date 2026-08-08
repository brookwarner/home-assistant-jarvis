from __future__ import annotations
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Awaitable, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

DEFAULT_ALERTS_PATH = str(Path(__file__).parent / "user_alerts.json")

# Key entities to watch in the insight polling loop
# `person` carries home/not_home/zone and is what lets the briefing reason about the
# household's day rather than just the house's readings. device_tracker is deliberately left
# out: it's the noisy raw layer (battery monitors, stale iPads) that person already aggregates.
WATCHED_DOMAINS = ["sensor", "binary_sensor", "switch", "climate", "lock", "person"]

# Domains where any state change is meaningful (no noise filtering)
BINARY_DOMAINS = {"binary_sensor", "switch", "lock", "input_boolean"}

# --- Proactive attention is OPT-IN -------------------------------------------------
# The old loop watched ALL of WATCHED_DOMAINS and woke the model on any drift, so
# 9-19 noisy sensor changes per 15-min poll each cost a Sonnet call. Now only an
# explicit allow-list of entities can trigger a proactive wake-up. Interactive
# questions still see everything; this only gates UNPROMPTED attention.
#
# The allow-list is configurable from the add-on Configuration screen via the
# PROACTIVE_WATCH / PROACTIVE_WATCH_DOMAINS env vars (comma-separated). Empty/unset
# falls back to these sensible defaults.
_DEFAULT_WATCH_SUBSTRINGS = (
    "garage_door", "front_door", "back_door",
    "moisture", "leak", "smoke", "water_sensor",
    "caravan_temperature",
)
_DEFAULT_WATCH_DOMAINS = ("lock",)


def _csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    items = tuple(s.strip() for s in raw.split(",") if s.strip())
    return items or default


WATCHED_ENTITY_SUBSTRINGS = _csv_env("PROACTIVE_WATCH", _DEFAULT_WATCH_SUBSTRINGS)
WATCHED_FULL_DOMAINS = set(_csv_env("PROACTIVE_WATCH_DOMAINS", _DEFAULT_WATCH_DOMAINS))
_WATCH_RE = re.compile("|".join(re.escape(s) for s in WATCHED_ENTITY_SUBSTRINGS))


def _is_watched(eid: str) -> bool:
    """True if this entity is allowed to trigger an unprompted proactive notification
    by pattern (substring) or whole-domain rules. Group membership is checked separately
    (it needs a live HA lookup) in _get_watch_group_members()."""
    domain = eid.split(".")[0] if "." in eid else ""
    if domain in WATCHED_FULL_DOMAINS:
        return True
    return bool(_WATCH_RE.search(eid))


# A HA Group helper is the user-friendly way to curate exactly which entities wake the
# model: they add entities via HA's native entity picker, and we read the group's members
# live each poll (no add-on restart). Configurable; empty disables the group path.
PROACTIVE_WATCH_GROUP = os.environ.get("PROACTIVE_WATCH_GROUP", "group.jarvis_watch").strip()


async def _get_watch_group_members(ha_client: Any) -> set[str]:
    """Live member entity_ids of the configured watch group, or empty set if the group
    is unset/missing/unreadable (graceful fallback to substring + domain rules)."""
    if not PROACTIVE_WATCH_GROUP:
        return set()
    try:
        state = await ha_client.get_state(PROACTIVE_WATCH_GROUP)
        members = (state or {}).get("attributes", {}).get("entity_id") or []
        return set(members)
    except Exception:
        return set()

# --- Operating modes ---------------------------------------------------------------
# A mode is a preset over: poll cadence, extra watched entities (merged with the
# allow-list), and the speak-threshold posture injected into the proactive prompt.
# Source of truth is a HA input_select read live each poll; falls back to DEFAULT_MODE.
DEFAULT_MODE = (os.environ.get("DEFAULT_MODE", "standard").strip().lower() or "standard")
MODE_ENTITY = os.environ.get("MODE_ENTITY", "input_select.jarvis_mode").strip()

MODES: dict[str, dict] = {
    "quiet": {
        "poll_min": 30,
        "extra_substrings": (),
        "extra_domains": (),
        "posture": "MODE quiet: only interrupt for a genuine safety emergency "
                   "(leak, smoke, security/intrusion). Stay silent about everything else.",
    },
    "standard": {
        "poll_min": None,  # inherit config.POLL_INTERVAL_MIN
        "extra_substrings": (),
        "extra_domains": (),
        "posture": "MODE standard: normal vigilance — speak only when something genuinely "
                   "warrants interrupting.",
    },
    "away": {
        "poll_min": 5,
        "extra_substrings": ("door", "window", "motion", "lock"),
        "extra_domains": ("lock",),
        "posture": "MODE away — nobody is home: treat any door/window/motion/lock/leak/smoke "
                   "event as notify-worthy and be security-vigilant.",
    },
    "storm": {
        "poll_min": 5,
        "extra_substrings": ("window", "door", "power", "wind", "weather"),
        "extra_domains": (),
        "posture": "MODE storm — severe weather: flag open windows/doors, power issues, and "
                   "weather-exposed conditions readily.",
    },
}


def _standard_poll_min() -> int:
    try:
        from jarvis.config import config
        return max(1, int(config.POLL_INTERVAL_MIN))
    except Exception:
        return 15


def _mode_poll_min(mode: str) -> int:
    m = MODES.get(mode, MODES["standard"])
    return m["poll_min"] if m["poll_min"] is not None else _standard_poll_min()


def _in_quiet_window(hour: int) -> bool:
    """True if the given local hour is inside the proactive quiet window.
    START==END disables the window; START>END is an overnight wrap."""
    from jarvis.config import config
    start, end = config.PROACTIVE_QUIET_START, config.PROACTIVE_QUIET_END
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # overnight wrap


async def resolve_mode(ha_client: Any) -> str:
    """Active mode from MODE_ENTITY (a HA input_select), validated against MODES.
    Falls back to DEFAULT_MODE when the entity is missing/unreadable/unknown."""
    fallback = DEFAULT_MODE if DEFAULT_MODE in MODES else "standard"
    if not MODE_ENTITY:
        return fallback
    try:
        state = await ha_client.get_state(MODE_ENTITY)
        val = ((state or {}).get("state") or "").strip().lower()
        return val if val in MODES else fallback
    except Exception:
        return fallback


def _is_watched_in_mode(eid: str, mode: str) -> bool:
    """Allow-list check including the active mode's extra watched substrings/domains."""
    if _is_watched(eid):
        return True
    m = MODES.get(mode, MODES["standard"])
    domain = eid.split(".")[0] if "." in eid else ""
    if domain in m["extra_domains"]:
        return True
    return any(s in eid for s in m["extra_substrings"])


# Appended to the morning briefing (when CARAVAN_PROMPT_ENABLED) so the user can opt the
# caravan auto-heat on for the day with a plain reply. The conversation agent has context
# (the briefing is recorded into history) and a set_caravan_heating tool to act on it.
CARAVAN_QUESTION = (
    "One more thing — are you planning to use the caravan today? "
    "If so, say the word and I'll switch on the auto-heat and its heartbeats."
)


# Numeric noise thresholds — change must exceed EITHER to be reported
NUMERIC_ABS_THRESHOLD = 2.0   # absolute units
NUMERIC_PCT_THRESHOLD = 0.05  # 5% relative change

# Module-level state snapshot for diff tracking
_last_snapshot: dict[str, str] = {}
# Monotonic timestamp of the last proactive poll that actually ran (for per-mode cadence).
_last_proactive_run: float | None = None


def _is_numeric(value: str) -> bool:
    try:
        float(value)
        return True
    except (ValueError, TypeError):
        return False


def compute_state_diff(
    states: list[dict],
    last_snapshot: dict[str, str],
    domains: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """
    Compare current states against last_snapshot.

    Returns:
        (new_snapshot, diff_lines) where diff_lines is a list of human-readable
        change descriptions. Empty list means nothing noteworthy changed.
    """
    watched = set(domains) if domains else None
    snapshot: dict[str, str] = {}

    for entity in states:
        eid = entity.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else ""
        if watched and domain not in watched:
            continue
        snapshot[eid] = entity.get("state", "")

    # If no baseline, this is first run — store snapshot, no diff
    if not last_snapshot:
        return snapshot, []

    diff: list[str] = []

    # Check for changes and new entities
    for eid, new_val in snapshot.items():
        if eid not in last_snapshot:
            diff.append(f"{eid}: new entity ({new_val})")
            continue

        old_val = last_snapshot[eid]
        if old_val == new_val:
            continue

        domain = eid.split(".")[0] if "." in eid else ""

        # Non-numeric or unavailable transitions: always report
        if not _is_numeric(new_val) or not _is_numeric(old_val):
            diff.append(f"{eid}: {old_val} -> {new_val}")
            continue

        # Binary domains: any change is meaningful
        if domain in BINARY_DOMAINS:
            diff.append(f"{eid}: {old_val} -> {new_val}")
            continue

        # Numeric: filter out noise
        old_f = float(old_val)
        new_f = float(new_val)
        abs_change = abs(new_f - old_f)
        pct_change = abs_change / abs(old_f) if old_f != 0 else float("inf")

        if abs_change >= NUMERIC_ABS_THRESHOLD or pct_change >= NUMERIC_PCT_THRESHOLD:
            diff.append(f"{eid}: {old_val} -> {new_val}")

    # Check for removed entities
    for eid in last_snapshot:
        if eid not in snapshot:
            diff.append(f"{eid}: removed")

    return snapshot, diff


async def check_user_alerts(
    ha_client: Any,
    on_trigger: Callable[[str], Awaitable[None]],
    alerts_path: str = DEFAULT_ALERTS_PATH,
) -> None:
    try:
        alerts = json.loads(Path(alerts_path).read_text()) if Path(alerts_path).exists() else []
    except Exception as e:
        logger.warning(f"Could not load user_alerts.json: {e}")
        return

    for alert in alerts:
        if not alert.get("enabled", True):
            continue
        try:
            state_data = await ha_client.get_state(alert["entity_id"])
            value = float(state_data.get("state", 0))
            threshold = float(alert["threshold"])
            condition = alert["condition"]

            triggered = (
                (condition == "above" and value > threshold)
                or (condition == "below" and value < threshold)
                or (condition == "equals" and value == threshold)
            )
            if triggered:
                await on_trigger(f"Alert: {alert['message']} ({alert['entity_id']}: {value})")
        except Exception as e:
            logger.debug(f"Alert check failed for {alert.get('entity_id')}: {e}")


async def run_morning_briefing(
    ha_client: Any,
    send_fn: Callable[[str], Awaitable[None]],
    briefing_recorder: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Generate and deliver the morning briefing. Shared by the scheduled 07:30 job and the
    manual /briefing command so both exercise the same flow: append the caravan question
    (arming the safety net), send, and record into history. Returns the sent text."""
    from jarvis.agents import briefing as briefing_agent
    from jarvis.anomaly import detect_and_surface
    from jarvis.config import config

    states = await ha_client.get_states()
    summary = ha_client.get_state_summary(
        states, domains=WATCHED_DOMAINS, exclude=config.BRIEFING_EXCLUDE_ENTITIES
    )
    anomalies = await detect_and_surface(ha_client)  # [] on any failure
    water_context = await briefing_agent.fetch_water_context(ha_client)  # None on any failure
    # Today's commitments, so the briefing can talk about the day rather than only the house.
    # Best-effort: a calendar outage degrades the briefing, it never cancels it.
    try:
        calendar_context = await briefing_agent.fetch_calendar_context(ha_client, states)
    except Exception as e:
        logger.debug(f"calendar context unavailable: {e}")
        calendar_context = None
    # Only ground the briefing in Watercare figures on a day water is actually newsworthy.
    # Once a standing water deviation has habituated out of the anomaly list, drop the water
    # block too — otherwise Jarvis keeps getting handed the figures and keeps leading with them.
    if water_context and not any("water" in a.lower() for a in anomalies):
        water_context = None
    text = await briefing_agent.generate(
        summary,
        anomalies=anomalies,
        water_context=water_context,
        calendar_context=calendar_context,
    )
    if config.CARAVAN_PROMPT_ENABLED:
        from jarvis import caravan
        text = text.rstrip() + "\n\n" + CARAVAN_QUESTION
        caravan.mark_prompt_sent()  # arms the safety net for today
    await send_fn(text)
    # Record into conversation history so a later reply ("yep, using it") has context
    # and the agent can act on it. Best-effort — never fail the briefing.
    if briefing_recorder is not None:
        try:
            await briefing_recorder(text)
        except Exception as e:
            logger.debug(f"briefing_recorder failed: {e}")
    return text


def build_scheduler(
    ha_client: Any,
    triage_agent_fn: Callable,
    briefing_agent_fn: Callable,
    send_fn: Callable[[str], Awaitable[None]],
    poll_interval: int = 15,
    briefing_recorder: Callable[[str], Awaitable[None]] | None = None,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    async def morning_briefing():
        logger.info("Running morning briefing")
        try:
            await run_morning_briefing(ha_client, send_fn, briefing_recorder=briefing_recorder)
        except Exception as e:
            logger.error(f"Morning briefing failed: {e}")
            try:
                await send_fn(f"Morning briefing failed: {e}")
            except Exception:
                pass

    async def caravan_safety_net():
        """If the morning briefing asked about the caravan but the user never gave an
        explicit answer, force the auto-heat off so an unused caravan never heats. No-op
        (and silent) when the user already decided, or when heating is already off."""
        try:
            from jarvis.config import config
            from jarvis import caravan
            if not config.CARAVAN_PROMPT_ENABLED or not caravan.decision_pending():
                return
            # Only notify if heating is actually on; a force-off when already off is silent.
            was_on = False
            primary = config.CARAVAN_ENTITIES[0] if config.CARAVAN_ENTITIES else None
            if primary:
                state = await ha_client.get_state(primary)
                was_on = ((state or {}).get("state") or "").strip().lower() == "on"
            await caravan.set_caravan(ha_client, enabled=False)
            caravan.mark_decided()  # don't re-enforce later the same day
            if was_on:
                await send_fn("No word on the caravan, so I've switched the auto-heat back off.")
        except Exception as e:
            logger.debug(f"caravan_safety_net failed: {e}")

    async def insight_poll():
        global _last_snapshot, _last_proactive_run
        try:
            await check_user_alerts(ha_client, send_fn)

            # Resolve the active operating mode and enforce its cadence. The job fires at
            # a fast base interval; we skip ticks that fall inside the mode's window.
            mode = await resolve_mode(ha_client)
            now = time.monotonic()
            if _last_proactive_run is not None and (now - _last_proactive_run) < _mode_poll_min(mode) * 60:
                return
            _last_proactive_run = now

            import datetime as _dt
            if _in_quiet_window(_dt.datetime.now().hour):
                states = await ha_client.get_states()
                group_members = await _get_watch_group_members(ha_client)
                watched_states = [
                    s for s in states
                    if _is_watched_in_mode(s.get("entity_id", ""), mode)
                    or s.get("entity_id", "") in group_members
                ]
                _last_snapshot, _ = compute_state_diff(watched_states, _last_snapshot)
                logger.debug("insight_poll: quiet window, snapshot refreshed, no model call")
                return

            states = await ha_client.get_states()
            # Opt-in: an entity may trigger an unprompted notification if it's in the
            # watch Group, matches the allow-list, or is in the active mode's extra-watch.
            group_members = await _get_watch_group_members(ha_client)
            watched_states = [
                s for s in states
                if _is_watched_in_mode(s.get("entity_id", ""), mode)
                or s.get("entity_id", "") in group_members
            ]
            # watched_states is already curated, so no extra domain filter here (lets
            # mode entities like weather.* through).
            new_snapshot, diff = compute_state_diff(watched_states, _last_snapshot)
            _last_snapshot = new_snapshot

            if not diff:
                logger.debug(f"insight_poll[{mode}]: no watched-entity changes, no model call")
                return

            posture = MODES.get(mode, MODES["standard"])["posture"]
            diff_text = f"{posture}\n\nHome state changes:\n" + "\n".join(diff)
            logger.info(f"insight_poll[{mode}]: {len(diff)} watched changes detected")
            logger.debug("insight_poll diff:\n" + diff_text)
            await triage_agent_fn(diff_text)
        except Exception as e:
            logger.debug(f"Insight poll error: {e}")

    # Daily briefing at 07:30 local time (scheduler uses system time — set TZ env var).
    # Gated on BRIEFING_ENABLED; the manual /briefing command runs the same flow regardless.
    from jarvis.config import config as _bcfg
    if _bcfg.BRIEFING_ENABLED:
        scheduler.add_job(morning_briefing, "cron", hour=7, minute=30, id="morning_briefing")
    else:
        logger.info("Morning briefing disabled (BRIEFING_ENABLED=false); skipping 07:30 job")

    # Caravan safety net: force auto-heat off if unanswered by CARAVAN_SAFETY_HOUR (default
    # 09:00). The job self-gates on config + whether a decision is still pending today.
    from jarvis.config import config as _cfg
    safety_hour = max(0, min(23, _cfg.CARAVAN_SAFETY_HOUR))
    scheduler.add_job(caravan_safety_net, "cron", hour=safety_hour, minute=0, id="caravan_safety_net")

    # Insight poll fires at a fast base cadence; per-mode cadence is enforced inside via
    # _last_proactive_run gating (away/storm 5 min, standard = poll_interval, quiet 30).
    base_interval = max(1, min(5, poll_interval))
    scheduler.add_job(insight_poll, "interval", minutes=base_interval, id="insight_poll")

    return scheduler
