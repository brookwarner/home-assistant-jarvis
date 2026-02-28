from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Callable, Awaitable, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

DEFAULT_ALERTS_PATH = str(Path(__file__).parent / "user_alerts.json")

# Key entities to watch in the insight polling loop
WATCHED_DOMAINS = ["sensor", "binary_sensor", "switch", "climate", "lock"]

# Domains where any state change is meaningful (no noise filtering)
BINARY_DOMAINS = {"binary_sensor", "switch", "lock", "input_boolean"}

# Numeric noise thresholds — change must exceed EITHER to be reported
NUMERIC_ABS_THRESHOLD = 2.0   # absolute units
NUMERIC_PCT_THRESHOLD = 0.05  # 5% relative change

# Module-level state snapshot for diff tracking
_last_snapshot: dict[str, str] = {}


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


def build_scheduler(
    ha_client: Any,
    triage_agent_fn: Callable,
    briefing_agent_fn: Callable,
    send_fn: Callable[[str], Awaitable[None]],
    poll_interval: int = 15,
) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    async def morning_briefing():
        logger.info("Running morning briefing")
        try:
            states = await ha_client.get_states()
            summary = ha_client.get_state_summary(states, domains=WATCHED_DOMAINS)
            from jarvis.agents.briefing import generate
            text = await generate(summary)
            await send_fn(text)
        except Exception as e:
            logger.error(f"Morning briefing failed: {e}")
            try:
                await send_fn(f"Morning briefing failed: {e}")
            except Exception:
                pass

    async def insight_poll():
        global _last_snapshot
        try:
            await check_user_alerts(ha_client, send_fn)

            states = await ha_client.get_states()
            new_snapshot, diff = compute_state_diff(
                states, _last_snapshot, domains=WATCHED_DOMAINS
            )
            _last_snapshot = new_snapshot

            if not diff:
                logger.debug("insight_poll: no state changes, skipping triage")
                return

            diff_text = "\n".join(diff)
            logger.info(f"insight_poll: {len(diff)} changes detected, calling triage")
            await triage_agent_fn(diff_text)
        except Exception as e:
            logger.debug(f"Insight poll error: {e}")

    # Daily briefing at 07:30 local time (scheduler uses system time — set TZ env var)
    scheduler.add_job(morning_briefing, "cron", hour=7, minute=30, id="morning_briefing")

    # Insight poll every poll_interval minutes (default 15)
    scheduler.add_job(insight_poll, "interval", minutes=poll_interval, id="insight_poll")

    return scheduler
