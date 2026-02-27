# Jarvis — AI Home Assistant for Home Assistant OS

A Telegram-based AI assistant that lives inside your Home Assistant instance.
Ask it questions, control devices, get morning briefings, and let it monitor your home.

Built on a Raspberry Pi running [Home Assistant OS](https://www.home-assistant.io/installation/raspberrypi).

---

## Features

- **Conversational control** — ask natural language questions, get live sensor data, control devices
- **Morning briefings** — daily summary of home state, weather, energy, water, calendar
- **Proactive alerts** — monitors sensors and notifies you when thresholds are crossed
- **Energy & water stats** — queries HA's long-term statistics database for any recorder statistic
- **Self-editing** — Jarvis can update his own personality (`soul.md`), briefing prompt, and HA automations
- **Voice support** — send voice messages, transcribed via Whisper (optional)
- **Persistent memory** — remembers facts and preferences across conversations
- **Opus sub-agent** — delegates complex tasks to Claude Opus 4.6

---

## Architecture

```
Telegram ──► bot.py ──► triage.py       (cheap model: classify message type)
                    ──► conversation.py  (main agent: tools + memory)
                    ──► briefing.py      (scheduled morning briefing)
                    ──► scheduler.py     (APScheduler: briefings, insight polls)
                    ──► webhook_server.py (receives HA events via rest_command)
```

**Models used (via OpenRouter):**
- Triage: `llama-3.2-3b-instruct:free` — routes messages, near-zero cost
- Conversation & Briefing: `claude-haiku-4.5` — fast, cheap, capable
- Opus sub-agent: `claude-opus-4.6` — delegated for complex reasoning tasks

**Tools available to the conversation agent:**
- `get_state` / `get_states_by_domain` — live entity states
- `get_history` — historical state changes
- `search_entities` — keyword search of known entities
- `search_statistics` — discover long-term statistic IDs
- `get_statistics` — query HA recorder statistics DB (energy, water, etc.)
- `call_service` — control devices
- `remember` — write to persistent memory
- `read_self` / `write_self` — edit soul.md, briefing_prompt.md, ha_entities.md
- `read_ha_config` / `write_ha_config` / `reload_ha_config` — edit HA YAML
- `add_custom_alert` — set up threshold monitors
- `delegate_to_opus` — hand complex tasks to Opus sub-agent

---

## Setup

### 1. Prerequisites

- Home Assistant OS (tested on 2026.x)
- Python 3.12+ with venv
- A Telegram bot token ([@BotFather](https://t.me/BotFather))
- An [OpenRouter](https://openrouter.ai) API key (or Anthropic direct key)
- A HA long-lived access token

### 2. Install

```bash
cd /homeassistant
git clone https://github.com/yourname/jarvis.git jarvis
cd jarvis
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
# Edit .env with your tokens, HA URL, and model choices
```

### 4. Run onboarding

The onboarding script generates all instance-specific files interactively:

```bash
.venv/bin/python scripts/onboard.py
```

It will ask about your name, pronouns, location, home features, personality style, and API keys, then generate:
- `.env` — all credentials and configuration
- `soul.md` — personalised AI personality (written by an LLM based on your answers)
- `ha_entities.md` — entity reference (auto-pulled from your HA instance)
- `briefing_prompt.md` — morning briefing instructions

Or configure manually by copying the example files:
```bash
cp .env.example .env          # then edit with your values
cp soul.example.md soul.md    # then customise
cp ha_entities.example.md ha_entities.md
```

### 5. Home Assistant configuration

Add to `configuration.yaml`:

```yaml
shell_command:
  restart_jarvis: /homeassistant/jarvis/start.sh
```

Add to `automations.yaml` (or via the HA UI):

```yaml
# Auto-start Jarvis on HA boot
- id: jarvis_autostart
  alias: "Jarvis: auto-start on HA boot"
  trigger:
    - platform: homeassistant
      event: start
  action:
    - delay: "00:00:10"
    - service: shell_command.restart_jarvis
  mode: single

# Forward HA entity unavailable alerts to Jarvis
- id: jarvis_entity_unavailable
  alias: "Jarvis: notify on key sensor unavailable"
  trigger:
    - platform: state
      entity_id:
        - sensor.your_important_sensor
      to: "unavailable"
      for: "00:05:00"
  action:
    - service: rest_command.jarvis_event
      data:
        title: "Sensor unavailable"
        message: "{{ trigger.entity_id }} has been unavailable for 5 minutes"

# Forward HA persistent notifications to Jarvis
- id: jarvis_ha_persistent_notification
  alias: "Jarvis: forward persistent notifications"
  trigger:
    - platform: event
      event_type: call_service
      event_data:
        domain: persistent_notification
        service: create
  action:
    - service: rest_command.jarvis_event
      data:
        title: "{{ trigger.event.data.service_data.get('title', 'HA Notification') }}"
        message: >-
          {{ trigger.event.data.service_data.get('message', '') }}
```

Add the webhook endpoint to `configuration.yaml`:

```yaml
rest_command:
  jarvis_event:
    url: "http://localhost:8765/alert"
    method: POST
    content_type: "application/json"
    payload: >-
      {"title": "{{ title }}", "message": "{{ message }}"}
    timeout: 10
```

After editing YAML, validate and reload:
```bash
ha core check && ha core restart
```

### 6. Start

```bash
bash start.sh
# or: PYTHONPATH=/homeassistant /homeassistant/jarvis/.venv/bin/python /homeassistant/jarvis/bot.py
```

The bot will send "{BOT_NAME} online. How can I help?" to your Telegram chat on startup.

---

## Personalisation

### soul.md
Defines Jarvis's personality, tone, and what he knows about you.
Loaded fresh on every message — edit and it takes effect immediately.
See `soul.example.md` for a starting template.

### ha_entities.md
A reference file of your home's entity IDs and what they are.
Used by the `search_entities` tool so Jarvis can find the right entity ID without guessing.
Format: `entity_id — Description (integration)`

### briefing_prompt.md
The system prompt for morning briefings. Jarvis can edit this himself via `write_self`.

### memory.md
Persistent memory. Jarvis appends facts here when you tell him to remember something.
Auto-created on first `remember` call.

---

## Telegram commands

| Command | Description |
|---|---|
| `/briefing` | Trigger an immediate morning briefing |
| *(any message)* | Chat with Jarvis |
| *(voice message)* | Transcribed and processed (requires Whisper) |

---

## Long-term statistics

Jarvis queries HA's recorder database directly for long-term statistics
(entities like `meridian_energy:consumption_day` that aren't in the states table).

Supported via `search_statistics` + `get_statistics` tools. Works for any
external statistic registered with HA's recorder — energy, water, custom integrations.

---

## Running tests

```bash
cd /homeassistant/jarvis
.venv/bin/pytest tests/ -v
```

---

## Cost

With the default model configuration (Haiku for conversation/briefings, free Llama for triage):
- **Conversation**: ~$0.001–0.005 per exchange (Haiku via OpenRouter)
- **Morning briefing**: ~$0.002–0.01 per briefing
- **Triage**: essentially free (llama-3.2-3b:free)
- **Opus sub-agent**: ~$0.05–0.20 per delegation (use sparingly)

Running costs are charged to your OpenRouter account, not Anthropic directly.

---

## Contributing

PRs welcome. The main things that would make this more useful for others:

- Onboarding script to auto-generate `soul.md` and `ha_entities.md`
- More tool examples (calendar integration, notification channels)
- Better test coverage
- Docker / add-on packaging
