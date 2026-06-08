#!/bin/sh
set -e
# Run the deployed code in /config/jarvis with the package importable as `jarvis`.
cd /config/jarvis
export PYTHONPATH=/config

# Translate the add-on Configuration options (/data/options.json) into env vars BEFORE
# launching the bot. config.py's load_dotenv() does not override existing env vars, so
# any option the user sets in the UI wins over .env; blank options fall back to .env.
if [ -f /data/options.json ]; then
  eval "$(python3 - <<'PY'
import json, shlex
try:
    o = json.load(open('/data/options.json'))
except Exception:
    o = {}
scalars = {
    'log_level': 'LOG_LEVEL', 'proactive_enabled': 'PROACTIVE_ENABLED',
    'proactive_poll_minutes': 'POLL_INTERVAL_MIN', 'bot_name': 'BOT_NAME',
    'owner_name': 'OWNER_NAME', 'timezone': 'TIMEZONE', 'whisper_model': 'WHISPER_MODEL',
    'triage_model': 'TRIAGE_MODEL', 'briefing_model': 'BRIEFING_MODEL',
    'conversation_model': 'CONVERSATION_MODEL', 'proactive_model': 'PROACTIVE_MODEL',
    'opus_model': 'OPUS_MODEL', 'telegram_bot_token': 'TELEGRAM_BOT_TOKEN',
    'telegram_chat_id': 'TELEGRAM_CHAT_ID', 'ha_url': 'HA_URL', 'ha_token': 'HA_TOKEN',
    'openrouter_api_key': 'OPENROUTER_API_KEY', 'groq_api_key': 'GROQ_API_KEY',
}
lists = {
    'proactive_watch_substrings': 'PROACTIVE_WATCH',
    'proactive_watch_domains': 'PROACTIVE_WATCH_DOMAINS',
}
out = []
for k, e in scalars.items():
    v = o.get(k)
    if isinstance(v, bool):
        v = 'true' if v else 'false'
    if v is not None and str(v) != '':
        out.append(f'export {e}={shlex.quote(str(v))}')
for k, e in lists.items():
    v = o.get(k)
    if v:
        out.append(f'export {e}={shlex.quote(",".join(str(x) for x in v))}')
print("\n".join(out))
PY
)"
fi

echo "[jarvis-addon] starting bot.py with $(python --version)"
exec python /config/jarvis/bot.py
