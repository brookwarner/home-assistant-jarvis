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
    'proactive_poll_minutes': 'POLL_INTERVAL_MIN', 'proactive_watch_group': 'PROACTIVE_WATCH_GROUP',
    'default_mode': 'DEFAULT_MODE', 'mode_entity': 'MODE_ENTITY',
    'bot_name': 'BOT_NAME', 'owner_name': 'OWNER_NAME',
    'whisper_model': 'WHISPER_MODEL',
    'caravan_prompt_enabled': 'CARAVAN_PROMPT_ENABLED',
}
lists = {
    'proactive_watch_substrings': 'PROACTIVE_WATCH',
    'proactive_watch_domains': 'PROACTIVE_WATCH_DOMAINS',
    'caravan_automations': 'CARAVAN_AUTOMATIONS',
}
# Grouped (nested) options.
models = o.get('models') or {}
secrets = o.get('secrets') or {}
nested = {
    ('models', 'triage'): 'TRIAGE_MODEL', ('models', 'briefing'): 'BRIEFING_MODEL',
    ('models', 'conversation'): 'CONVERSATION_MODEL', ('models', 'proactive'): 'PROACTIVE_MODEL',
    ('models', 'opus'): 'OPUS_MODEL',
    ('secrets', 'telegram_bot_token'): 'TELEGRAM_BOT_TOKEN',
    ('secrets', 'telegram_chat_id'): 'TELEGRAM_CHAT_ID', ('secrets', 'ha_url'): 'HA_URL',
    ('secrets', 'ha_token'): 'HA_TOKEN', ('secrets', 'openrouter_api_key'): 'OPENROUTER_API_KEY',
    ('secrets', 'groq_api_key'): 'GROQ_API_KEY',
}
groups = {'models': models, 'secrets': secrets}

out = []

def emit(env, val):
    if isinstance(val, bool):
        val = 'true' if val else 'false'
    if val is not None and str(val) != '':
        out.append(f'export {env}={shlex.quote(str(val))}')

for k, e in scalars.items():
    emit(e, o.get(k))
for (grp, key), e in nested.items():
    emit(e, groups.get(grp, {}).get(key))
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
