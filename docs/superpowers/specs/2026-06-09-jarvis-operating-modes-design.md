# Jarvis Operating Modes — Design

**Date:** 2026-06-09
**Status:** Approved

## Goal

Give Jarvis explicit operating postures (quiet / standard / away / storm) that vary his proactive intensity and notification policy. Manual control now, with the design "auto-ready" so HA automations can drive the mode later with no Jarvis changes.

## Principle

A mode is **not a new subsystem** — it is a preset over the levers built in the noise/config refactor: proactive cadence, which entities wake the heartbeat (the allow-list), and the proactive speak-threshold posture. Each mode is one row in a policy table.

## Modes (the policy table)

| mode | effective poll | extra watched (merged with the user's watch group + substrings + domains) | speak-threshold posture |
|---|---|---|---|
| **quiet** | 30 min | none | Only interrupt for a genuine safety emergency (leak/smoke/security). Otherwise stay silent. |
| **standard** | configured `POLL_INTERVAL_MIN` (default 15) | none | Today's behaviour. |
| **away** | 5 min | `door`, `window`, `motion`, `lock` (substrings) + `lock` domain | Nobody home — treat any door/window/motion/lock/leak/smoke event as notify-worthy; be security-vigilant. |
| **storm** | 5 min | `window`, `door`, `power`, `wind`, `weather` (substrings) | Severe weather — flag open windows/doors, power issues, weather-exposed conditions readily. |

- **quiet is not absolutely silent:** it still surfaces genuine safety (leak/smoke/security). Confirmed.
- **Per-mode cadence is in scope:** quiet 30 / standard 15 / away & storm 5.
- Extra-watch entries are *added* to the user's curated watch group/substrings/domains, never replace them.

## Control — single source of truth

`input_select.jarvis_mode` (options: quiet, standard, away, storm). Jarvis reads it **live each poll** (same pattern as the watch group). The user creates the helper once (Settings → Devices & Services → Helpers → Dropdown). If the entity is missing/unreadable, Jarvis falls back to the configurable `default_mode` (= standard).

Because the control point is a plain HA entity, this is the **auto-ready** hook: any HA automation (presence → away, severe-weather entity → storm) can set the `input_select` later with zero Jarvis changes. Automatic triggers are explicitly **out of scope** for this card.

**Two ways to switch, both ending at that entity:**
- Set the `input_select` directly (dashboard / automation).
- Tell Jarvis in Telegram ("go quiet", "away mode", "set mode storm") → he calls `input_select.select_option` on `mode_entity` → HA and Jarvis stay in one truth → he confirms.

## What changes in the heartbeat (`insight_poll`)

1. Resolve the active mode (read `mode_entity`; fall back to `default_mode`).
2. **Interval gating:** the APScheduler job keeps firing at a fast base cadence; each tick skips unless `now - last_proactive_run >= mode.poll_min` (no job rescheduling).
3. Merge the mode's extra-watch substrings/domains into the allow-list set for this tick.
4. Inject the mode name + posture into the proactive context so the model's "is this worth interrupting?" bar matches the mode.

## Components / files

- `scheduler.py` — mode policy table (`MODES`), `resolve_mode(ha_client)`, interval gating + extra-watch merge in `insight_poll`.
- `config.py` — `DEFAULT_MODE` (default "standard"), `MODE_ENTITY` (default `input_select.jarvis_mode`).
- `agents/conversation.py` — a `set_mode` tool (calls `input_select.select_option`) and the mode/posture injected into the proactive system prompt.
- `bot.py` — pass mode posture into the proactive context (alongside the existing diff + recent-messages).
- `ha_client.py` — reuse `get_state` to read the mode entity; `call_service` to set it.
- `addon/config.yaml` + `run.sh` — `default_mode` (list) and `mode_entity` (str?) options.

## Testing

- `resolve_mode`: reads the entity's state; falls back to `default_mode` when absent/unreadable; rejects unknown values → default.
- Policy application: extra-watch substrings/domains merge into the watched set; posture string reaches the proactive context; interval gating skips ticks within the mode's window and allows them after.
- Telegram set-mode: maps phrases → `input_select.select_option` on `mode_entity`.

## Out of scope

Automatic mode switching (presence/weather/calendar) — deferred; the `input_select` hook makes it additive later. The `watchful` and `guest` modes from the card (watchful overlaps standard; guest is a privacy axis, not a proactivity one) — deferred.

## Deploy

Code lives in `/config/jarvis` (add-on restart picks it up); `config.yaml`/`run.sh` changes ship via `ha store reload` + `ha apps update` (version bump). The user creates the `input_select.jarvis_mode` helper.
