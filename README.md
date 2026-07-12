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

**Models used (direct Anthropic billing by default):**
- Triage: `claude-haiku-4-5` — routes messages, cheap and reliable
- Conversation & Briefing: `claude-haiku-4-5` — fast, cheap, capable
- Proactive polling: `claude-haiku-4-5` — evaluates state-change diffs (only when the local recommendation engine clears its score threshold)
- Opus sub-agent: `claude-opus-4-6` — delegated for complex reasoning tasks

Every model string is overridable via `.env` (`TRIAGE_MODEL`, `CONVERSATION_MODEL`, etc.). Set them to the `openrouter/...` equivalents if you'd rather route through OpenRouter. Per-call cost and token usage are logged (`jarvis.usage`), with a running session total.

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
- `set_mode` — switch operating mode (quiet/standard/away/storm)
- `check_anomalies` — on-demand anomaly check vs the learned baseline (same engine as the briefing)
- `recent_changes` — git log / recently-modified files for Jarvis's own code or the HA config
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
git clone https://github.com/brookwarner/home-assistant-jarvis.git jarvis
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

## Running as a Home Assistant add-on (optional)

Instead of launching `bot.py` yourself, you can run Jarvis as a **local Home
Assistant add-on** so the Supervisor manages it (auto-start on boot, watchdog
restart, config from the UI). The add-on image just installs the dependencies
and runs your cloned code from `/config/jarvis` — so you still clone the repo as
in step 2 above.

1. **Clone the code** into your HA config dir (as in step 2), so it lives at
   `/config/jarvis` (a.k.a. `/homeassistant/jarvis`).

2. **Copy the add-on definition** into the Supervisor's local add-ons folder:

   ```bash
   mkdir -p /addons/jarvis
   cp /homeassistant/jarvis/addon/* /addons/jarvis/
   ```

   (On HA OS the add-ons folder is `/addons`; via the Samba/SSH add-ons it is
   often surfaced as `/homeassistant/../addons` or the `addons` share.)

3. **Install it.** In Home Assistant go to **Settings → Add-ons → Add-on Store**,
   open the **⋮** menu → **Check for updates**, then scroll to **Local add-ons**.
   Open **Jarvis** and click **Install**.

4. **Configure.** Either fill in the add-on's **Configuration** tab (tokens live
   under the optional `secrets` group, models under `models`) **or** leave those
   blank and let it fall back to `/config/jarvis/.env`. UI options win over
   `.env` when both are set.

5. **Start** the add-on and enable **Start on boot** + **Watchdog**.

The add-on shares the host network so HA's `rest_command` (localhost:8765) reaches
the webhook and the bot reaches the HA API. See `addon/config.yaml` for every
exposed option.

> Note: the prebuilt Dockerfile targets `aarch64` (Raspberry Pi / ARM64). Adjust
> `arch:` in `addon/config.yaml` for other platforms.

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
| `/cost` | Show LLM spend: this session, today, month-to-date, and all-time |
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

With the default model configuration (Haiku for triage/conversation/briefing/proactive):
- **Conversation**: ~$0.001–0.005 per exchange (Haiku)
- **Morning briefing**: ~$0.002–0.01 per briefing
- **Triage**: ~$0.0001 per message (Haiku, ~10 output tokens)
- **Proactive polling**: ~$0.001–0.005 per poll cycle — only invokes a model when state changes *and* the local recommendation engine clears its score threshold; most cycles cost nothing
- **Opus sub-agent**: ~$0.05–0.20 per delegation (use sparingly)

Running costs are billed to your Anthropic account directly (no OpenRouter markup) under the default config. Per-call costs are logged by the `jarvis.usage` logger so you can see exactly where the money goes instead of reading it off the provider dashboard. Switching the proactive model to `claude-sonnet-4-6` raises its quality but costs ~10x more per invocation.

Cost is also tracked cumulatively — ask Jarvis `/cost` for this session's spend plus today/month-to-date/all-time totals. The latter three are persisted to `usage_state.json` (path overridable via `USAGE_STATE_PATH`) so they survive restarts, unlike the in-memory session total.

---

## Contributing

PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and conventions.
The main things that would make this more useful for others:

- More tool examples (calendar integration, notification channels)
- Better test coverage
- Polishing the Home Assistant add-on (`addon/`) for the community add-on store

## License

[MIT](LICENSE) — do what you like, no warranty. Bring your own API keys.
