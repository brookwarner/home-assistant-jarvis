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
        try:
            await check_user_alerts(ha_client, send_fn)
            # Run triage on a quick home snapshot for AI-initiated insights
            states = await ha_client.get_states()
            summary = ha_client.get_state_summary(states, domains=["binary_sensor", "switch", "sensor"])
            await triage_agent_fn(summary)
        except Exception as e:
            logger.debug(f"Insight poll error: {e}")

    # Daily briefing at 07:30 local time (scheduler uses system time — set TZ env var)
    scheduler.add_job(morning_briefing, "cron", hour=7, minute=30, id="morning_briefing")

    # Insight poll every 5 minutes
    scheduler.add_job(insight_poll, "interval", minutes=5, id="insight_poll")

    return scheduler
