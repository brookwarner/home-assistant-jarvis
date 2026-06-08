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
    TRIAGE_MODEL: str = os.environ.get(
        "TRIAGE_MODEL", "openrouter/meta-llama/llama-3.2-3b-instruct:free"
    )
    BRIEFING_MODEL: str = os.environ.get(
        "BRIEFING_MODEL", "openrouter/anthropic/claude-sonnet-4-6"
    )
    CONVERSATION_MODEL: str = os.environ.get(
        "CONVERSATION_MODEL", "openrouter/anthropic/claude-sonnet-4-6"
    )
    OPUS_MODEL: str = os.environ.get("OPUS_MODEL", "openrouter/anthropic/claude-opus-4.6")
    PROACTIVE_MODEL: str = os.environ.get("PROACTIVE_MODEL", "openrouter/anthropic/claude-sonnet-4-6")
    BOT_NAME: str = os.environ.get("BOT_NAME", "Jarvis")
    OWNER_NAME: str = os.environ.get("OWNER_NAME", "the user")
    TIMEZONE: str = os.environ.get("TIMEZONE", "UTC")
    WEBHOOK_PORT: int = int(os.environ.get("WEBHOOK_PORT", "8765"))
    WHISPER_MODEL: str = os.environ.get("WHISPER_MODEL", "base")
    LOG_LEVEL: str = os.environ.get("LOG_LEVEL", "INFO")
    # Proactive heartbeat controls (exposed in the add-on Configuration screen).
    PROACTIVE_ENABLED: bool = os.environ.get("PROACTIVE_ENABLED", "true").strip().lower() in (
        "true", "1", "yes", "on",
    )
    POLL_INTERVAL_MIN: int = int(os.environ.get("POLL_INTERVAL_MIN", "15"))

config = Config()
