import os
from pathlib import Path
from dotenv import load_dotenv

# Load THIS package's .env explicitly. A bare load_dotenv() walks up the tree and can
# pick up a different .env (e.g. a git worktree finding the deployed instance's file).
load_dotenv(Path(__file__).parent / ".env")

class Config:
    TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
    TELEGRAM_CHAT_ID: int = int(os.environ["TELEGRAM_CHAT_ID"])
    HA_URL: str = os.environ.get("HA_URL", "http://localhost:8123")
    HA_TOKEN: str = os.environ["HA_TOKEN"]
    ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")
    OPENROUTER_API_KEY: str = os.environ.get("OPENROUTER_API_KEY", "")
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
    # Default to direct Anthropic billing (no OpenRouter 5% markup, reliable prompt caching).
    # Set the openrouter/* equivalents in .env if you prefer routing through OpenRouter.
    TRIAGE_MODEL: str = os.environ.get(
        "TRIAGE_MODEL", "anthropic/claude-haiku-4-5"
    )
    BRIEFING_MODEL: str = os.environ.get(
        "BRIEFING_MODEL", "anthropic/claude-haiku-4-5"
    )
    CONVERSATION_MODEL: str = os.environ.get(
        "CONVERSATION_MODEL", "anthropic/claude-haiku-4-5"
    )
    OPUS_MODEL: str = os.environ.get("OPUS_MODEL", "anthropic/claude-opus-4-6")
    # Proactive polling runs unattended; Haiku keeps it ~10x cheaper than Sonnet and the
    # local recommendation engine already gates how often the model is woken at all.
    PROACTIVE_MODEL: str = os.environ.get("PROACTIVE_MODEL", "anthropic/claude-haiku-4-5")
    BOT_NAME: str = os.environ.get("BOT_NAME", "Jarvis")
    OWNER_NAME: str = os.environ.get("OWNER_NAME", "the user")
    # Blank = follow Home Assistant's configured timezone (fetched at startup in bot.py).
    # Set explicitly only to override HA.
    TIMEZONE: str = os.environ.get("TIMEZONE", "")
    WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", "8765"))
    WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base")
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    # Proactive heartbeat controls (exposed in the add-on Configuration screen).
    PROACTIVE_ENABLED: bool = os.environ.get("PROACTIVE_ENABLED", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )
    POLL_INTERVAL_MIN: int = int(os.environ.get("POLL_INTERVAL_MIN", "15"))
    # Overnight quiet window: during these local hours the proactive poll skips the
    # expensive model entirely (no unprompted nighttime notifications, no spend).
    # 0-23. START==END disables the window. START>END means an overnight wrap.
    PROACTIVE_QUIET_START: int = int(os.environ.get("PROACTIVE_QUIET_START", "23"))
    PROACTIVE_QUIET_END: int = int(os.environ.get("PROACTIVE_QUIET_END", "6"))
    # Caravan: the morning briefing can ask whether you'll use the caravan that day.
    # When you confirm, Jarvis enables these entities — the master heater toggle plus
    # the auto-heat automation (and any heartbeats). Each entity is switched using its
    # own domain (input_boolean.turn_on, automation.turn_off, ...). Comma-separated.
    CARAVAN_PROMPT_ENABLED: bool = os.environ.get("CARAVAN_PROMPT_ENABLED", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )
    CARAVAN_ENTITIES: list[str] = [
        s.strip()
        for s in os.environ.get(
            "CARAVAN_ENTITIES",
            "input_boolean.caravan_heater_enabled,automation.warm_caravan_2_minute_heartbeat",
        ).split(",")
        if s.strip()
    ]
    # Physical heater plugs the auto-heat automation switches on. Disabling the automation
    # only stops it managing them in future — it leaves any running heaters on. So when
    # caravan heating is turned OFF, Jarvis also force-switches these off now. They are NOT
    # turned on when enabling: the thermostat automation owns that decision. Comma-separated.
    CARAVAN_HEATER_SWITCHES: list[str] = [
        s.strip()
        for s in os.environ.get(
            "CARAVAN_HEATER_SWITCHES",
            "switch.zigbee_plug_1_office,switch.tz3000_typdpbpg_ts011f_2",
        ).split(",")
        if s.strip()
    ]
    # Safety net: if no explicit caravan decision is made by this local hour, the
    # auto-heat is forced off so an unused caravan never heats. 0-23; set via add-on.
    CARAVAN_SAFETY_HOUR: int = int(os.environ.get("CARAVAN_SAFETY_HOUR", "9"))
    # Post-enable power-draw verification. A few minutes after caravan heating is enabled,
    # Jarvis confirms the heaters are actually pulling watts. If the master toggle never
    # took, or the caravan is cold but drawing nothing, it self-heals (re-enable / switch
    # the plugs on directly) and tells the user — closing the gap where heating was
    # "enabled" in chat but the caravan stayed cold.
    CARAVAN_TEMP_SENSOR: str = os.environ.get(
        "CARAVAN_TEMP_SENSOR", "sensor.ths_caravan_temperature"
    )
    CARAVAN_POWER_SENSORS: list[str] = [
        s.strip()
        for s in os.environ.get(
            "CARAVAN_POWER_SENSORS",
            "sensor.zigbee_plug_1_office_power,sensor.tz3000_typdpbpg_ts011f_power_2",
        ).split(",")
        if s.strip()
    ]
    CARAVAN_MIN_HEATER_WATTS: float = float(os.environ.get("CARAVAN_MIN_HEATER_WATTS", "5"))
    # Only treat "no draw" as a fault when the caravan is below this temperature; above it
    # the heaters are legitimately idle, so staying silent avoids false alarms.
    CARAVAN_COMFORT_FLOOR_C: float = float(os.environ.get("CARAVAN_COMFORT_FLOOR_C", "16"))
    # Minutes to wait after enabling before the power-draw check runs (the thermostat
    # heartbeat fires every ~2 min, so give it a couple of cycles to actually switch on).
    CARAVAN_VERIFY_DELAY_MIN: float = float(os.environ.get("CARAVAN_VERIFY_DELAY_MIN", "5"))
    # Watercare smart-meter sensor. Its attributes (this home's own daily_average and the
    # Watercare household_efficiency_band) are surfaced into the briefing so any water
    # mention is grounded in real figures rather than a confabulated 'NZ average'.
    WATERCARE_SENSOR: str = os.environ.get("WATERCARE_SENSOR", "sensor.watercare")
    # Entities kept OUT of the morning-briefing state summary. Default drops
    # sensor.water_usage_vs_average — a template that compares this home's monthly-derived
    # daily figure to a HARDCODED 200 L/day "NZ Average" constant, yielding a fixed "174%".
    # Because it's monthly-cadence data, that value sat frozen for days and the briefing
    # re-headlined the same "174% above the NZ average" every morning. It is real sensor
    # data (not confabulated), but a dubious, stale comparison we don't want leading.
    BRIEFING_EXCLUDE_ENTITIES: list[str] = [
        s.strip()
        for s in os.environ.get(
            "BRIEFING_EXCLUDE_ENTITIES",
            "sensor.water_usage_vs_average",
        ).split(",")
        if s.strip()
    ]

config = Config()
