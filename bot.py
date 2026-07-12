from __future__ import annotations
import asyncio
import logging
import os
import re
import tempfile
from pathlib import Path


def _strip_markdown(text: str) -> str:
    """Remove common markdown formatting from text for plain Telegram messages."""
    # Remove headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bold/italic (**, __, *, _)
    text = re.sub(r'\*{1,3}(.+?)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,3}(.+?)_{1,3}', r'\1', text)
    # Remove inline code and code blocks
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0).replace('```', '').strip(), text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Remove table rows (lines containing |)
    text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-| :]+$', '', text, flags=re.MULTILINE)
    # Remove leading bullet markers
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.MULTILINE)
    # Collapse multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from jarvis.config import config
from jarvis.ha_client import HAClient
from jarvis.agents.conversation import ConversationAgent
from jarvis.agents.triage import classify
from jarvis.transcriber import transcribe
from jarvis.webhook_server import start_server
from jarvis.scheduler import build_scheduler, WATCHED_DOMAINS

logging.basicConfig(
    # Case-insensitive + safe default: add-on options use lowercase (info/debug/...),
    # .env historically used uppercase. getattr(logging, "info") would return the
    # logging.info *function*, not the level — so normalise and fall back to INFO.
    level=getattr(logging, str(config.LOG_LEVEL).upper(), logging.INFO),
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ha = HAClient(config.HA_URL, config.HA_TOKEN)
agent: ConversationAgent  # initialized in main()


async def send_to_user(text: str) -> None:
    """Send a message to the configured Telegram chat."""
    app = _app_ref[0]
    if app:
        await app.bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=text)


async def on_ha_event(event: dict) -> None:
    """Called by webhook server when HA fires an event."""
    try:
        states = await ha.get_states()
        context = ha.get_state_summary(states, domains=WATCHED_DOMAINS)
        action = await classify(event, context)
        logger.info(f"Triage decision: {action} for '{event.get('title', '')}'")

        if action in ("notify", "needs_input"):
            title = event.get("title", "")
            message = event.get("message", "")
            await agent.run_proactive(
                context=f"{title}: {message}",
                chat_id=config.TELEGRAM_CHAT_ID,
            )
        # "ignore" and "log" → do nothing (already logged above)
    except Exception as e:
        logger.error(f"on_ha_event failed: {e}")
        try:
            await send_to_user(f"Error handling event: {e}")
        except Exception:
            pass


async def _keep_typing(bot, chat_id: int) -> None:
    """Re-send typing action every 4s so it persists during long tool chains."""
    try:
        while True:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
        logger.warning(f"Ignoring message from unknown chat {update.effective_chat.id}")
        return
    user_text = update.message.text
    logger.info(f"Received text: {user_text[:80]}")

    # If agent is waiting for ask_user input, route this message there
    if agent._pending_reply is not None and not agent._pending_reply.done():
        agent._pending_reply.set_result(user_text)
        return

    # If agent is mid-task (not waiting for input), tell user to wait
    if agent._agent_busy:
        await update.message.reply_text("Still working on the last task, just a moment.")
        return

    typing_task = asyncio.create_task(_keep_typing(context.bot, update.effective_chat.id))
    try:
        reply = await agent.reply(chat_id=update.effective_chat.id, user_text=user_text)
    finally:
        typing_task.cancel()
    if reply and reply.strip():
        await update.message.reply_text(_strip_markdown(reply))


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
        return
    typing_task = asyncio.create_task(_keep_typing(context.bot, update.effective_chat.id))
    voice = update.message.voice
    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        await tg_file.download_to_drive(tmp_path)
        text = await transcribe(tmp_path)
        logger.info(f"Transcribed: {text[:80]}")
        reply = await agent.reply(chat_id=update.effective_chat.id, user_text=text)
        await update.message.reply_text(f"[{text}]\n\n{_strip_markdown(reply)}")
    finally:
        typing_task.cancel()
        Path(tmp_path).unlink(missing_ok=True)


async def _record_briefing(text: str) -> None:
    # Land the morning briefing (incl. the caravan question) in conversation history so the
    # user's reply has context and the agent can enable caravan heating on request.
    agent.note_briefing(config.TELEGRAM_CHAT_ID, text)


async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger an immediate morning briefing — useful for testing. Runs the same path as
    the scheduled 07:30 job (caravan question, history recording, safety-net arming)."""
    if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
        return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    try:
        from jarvis.scheduler import run_morning_briefing

        async def _send(t: str) -> None:
            await update.message.reply_text(_strip_markdown(t))

        await run_morning_briefing(ha, _send, briefing_recorder=_record_briefing)
    except Exception as e:
        await update.message.reply_text(f"Briefing failed: {e}")


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report LLM spend: this process's session, today, month-to-date, and all-time
    (the latter three persist across restarts — see jarvis/usage.py)."""
    if update.effective_chat.id != config.TELEGRAM_CHAT_ID:
        return
    from jarvis import usage

    def _line(label: str, t: dict) -> str:
        return f"{label}: {int(t['requests'])} calls, ${t['cost']:.4f} ({int(t['input_tokens'])} in / {int(t['output_tokens'])} out tok)"

    lines = [
        _line("Session", usage.session_totals()),
        _line("Today", usage.today_totals()),
        _line("This month", usage.month_to_date_totals()),
        _line("All-time", usage.lifetime_totals()),
    ]
    await update.message.reply_text("\n".join(lines))


_app_ref: list = [None]


async def main() -> None:
    # Follow Home Assistant's configured timezone unless TIMEZONE is explicitly set.
    # config.TIMEZONE is read dynamically everywhere, so setting it here propagates.
    if not config.TIMEZONE:
        try:
            config.TIMEZONE = await ha.get_timezone() or "UTC"
        except Exception as e:
            config.TIMEZONE = "UTC"
            logger.warning(f"Could not read HA timezone, defaulting to UTC: {e}")
        logger.info(f"Timezone (from Home Assistant): {config.TIMEZONE}")

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    _app_ref[0] = app
    global agent
    agent = ConversationAgent(ha, send_fn=send_to_user)

    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Start webhook server
    webhook_runner = await start_server(on_ha_event, config.WEBHOOK_PORT)

    # Start scheduler
    async def proactive_poll(diff_text: str) -> None:
        if not config.PROACTIVE_ENABLED:
            return  # proactive heartbeat turned off in the add-on Configuration screen
        recent = list(agent._recent_alerts)
        context_parts = [f"Home state changes since last poll:\n{diff_text}"]
        # When the caravan temperature is what moved, the agent has no way to know whether
        # heating is *meant* to be on. A falling caravan temp while heating is OFF is the
        # expected result of the caravan being unused — not a fault. Hand the agent that
        # fact so it doesn't confabulate a "missing entity" / "broken automation" alarm.
        caravan_temp_sensor = (config.CARAVAN_TEMP_SENSOR or "").strip()
        if caravan_temp_sensor and caravan_temp_sensor in diff_text:
            enable_eid = config.CARAVAN_ENTITIES[0] if config.CARAVAN_ENTITIES else ""
            try:
                st = await ha.get_state(enable_eid) if enable_eid else None
                heating_on = ((st or {}).get("state") or "").strip().lower() == "on"
            except Exception:
                heating_on = None  # couldn't read — say so rather than guess
            if heating_on is False:
                context_parts.append(
                    "Caravan context: caravan auto-heating is currently OFF "
                    f"({enable_eid} = off), so the caravan is intentionally unheated and a "
                    "falling caravan temperature is the normal, expected consequence — NOT a "
                    "fault, a missing entity, or a broken automation. Stay SILENT about the "
                    "caravan unless the user has asked for heat."
                )
            elif heating_on is True:
                context_parts.append(
                    f"Caravan context: caravan auto-heating is ON ({enable_eid} = on). If the "
                    "temperature is climbing or steady, that is heating working normally."
                )
        if recent:
            context_parts.append(
                "Recent messages already sent (do NOT repeat their content):\n"
                + "\n".join(f"- {a}" for a in recent)
            )
        context = "\n\n".join(context_parts)
        await agent.run_proactive(
            context=context,
            chat_id=config.TELEGRAM_CHAT_ID,
            use_history=False,   # throwaway context — don't flood conversation history
            model=config.PROACTIVE_MODEL,
        )

    scheduler = build_scheduler(
        ha, proactive_poll, None, send_to_user,
        poll_interval=config.POLL_INTERVAL_MIN,
        briefing_recorder=_record_briefing,
    )
    scheduler.start()

    logger.info(f"{config.BOT_NAME} is online.")
    await send_to_user(f"{config.BOT_NAME} online. How can I help?")

    async with app:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        try:
            await asyncio.Event().wait()  # Run forever
        finally:
            await app.updater.stop()
            await app.stop()
            scheduler.shutdown()
            await webhook_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
