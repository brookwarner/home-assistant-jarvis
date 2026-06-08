# Jarvis Brief Refactor — Design

**Date:** 2026-06-08
**Status:** Approved (design); pending spec review
**Scope:** Local refactor only. Claude-routines re-platform is explicitly out of scope (deferred to a later project).

## Problem

Jarvis (the Telegram smart-home agent, BOT_NAME currently "bushbot") exhibits five user-reported problems:

1. **Repeats himself** — across proactive alerts and across daily briefings (e.g. "Water usage is 42.5 percent of average" reported verbatim on consecutive mornings; offline Tasmota devices, caravan battery, and backup time re-reported every day).
2. **Talks to himself** — the proactive heartbeat wakes a frontier model every 15 minutes and reasons out loud before deciding `SILENT` ~90% of the time.
3. **Sends useless info** — low-value content (iPad battery %, next-backup time, trivial weather wording changes) and noise from constantly-changing status-text entities.
4. **No personality** — the carefully written persona in `soul.md` essentially never reaches the output.
5. **Eats tokens for no reason** — the quiet 90% of polls still cost ~3K+ tokens each (≈300K tokens/day floor), with worst-case 50–100K-token blowups per poll.

These are quality/tuning problems in existing code, not missing features. The cmd-deck "Jarvis" board (project `6ab52286-d8ec-47b7-9441-36635dac26f9`) tracks 9 idle *feature* cards; none addresses these. This work is separate and should ship first.

## Root-Cause Findings (evidence-based)

- **Personality never lands because:**
  - The **morning briefing never loads `soul.md`** at all — it runs entirely off the standalone, voiceless `briefing_prompt.md` (`agents/briefing.py`). The one daily message is voiceless by construction.
  - **Conversations run on Haiku 4.5** (`CONVERSATION_MODEL`), too small to sustain a rich theatrical persona even when soul is loaded.
  - The only path that loads full soul *and* runs on a strong model (Sonnet) is the proactive heartbeat — silent ~90% of the time, so the voice has no stage.
  - When he does speak, the `base` operational block appended *after* soul.md mandates terseness ("First sentence is the answer. No filler. One sentence."), burying the character.
- **Repetition** is caused by the only dedup mechanism, `_recent_alerts`, keeping the last 5 messages **truncated to 200 chars** — too short to match, and the log literally shows him noting the truncation. Briefings have no cross-day memory at all.
- **Noise** is caused by the diff engine watching all of `sensor/binary_sensor/switch/climate/lock` with a 2.0-abs / 5% threshold, where non-numeric `input_text.attic_harvest_*` / `attic_intake_*` status text and weather `description` change every poll and always trip the diff.
- **Token bleed** is caused by: (a) full ~3K system prompt re-sent every 15-min poll regardless of outcome; (b) the `_run_with_tools` loop (`MAX_TOOL_ROUNDS=5`) re-sending accumulated context each round, and proactive runs calling `get_states` which dumps every entity; (c) prompt caching defeated because `{_now_str()}` (current time) is embedded at the top of the static system prompt, busting the cache on every call.

## Decisions (locked)

- **D1.** Local refactor first; routines deferred.
- **D2.** Scope = agent-brief refactor **plus** structural noise/dedup/token fixes.
- **D3.** Strengthen personality so it actually manifests; do **not** tone it down.
- **D4.** Promote `BRIEFING_MODEL` and `CONVERSATION_MODEL` to Sonnet 4.6.
- **D5.** Triage stays on the cheap free model for now (optional future bump to Haiku for better notify/ignore judgment — noted, not in scope).
- **D6.** Caching may require routing Sonnet paths direct to the Anthropic API instead of OpenRouter; final call deferred to planning.

## Design — Six Components

### 1. Restructure the agent brief
Replace the monolithic `_load_system_prompt()` (`soul → "---" → base → memory`) with three composable layers, assembled per path:
- **Voice layer** — `soul.md`, leads, owns *how he speaks*.
- **Operational layer** — tools, timezone, formatting. Rewritten to drop the terseness mandate ("answer-first / one sentence / be brief"). Keep the banned-filler-words rule. Brevity reframed as "don't pad," not "strip character."
- **Mode layer** — short per-path addendum: `proactive` (silence-default + when-to-speak), `conversation` (full voice), `briefing` (full voice + daily-summary shape).

The **briefing loads the same soul** + a briefing mode-note. `briefing_prompt.md` is demoted from "the whole prompt" to a mode addendum (or removed).

### 2. Reconcile soul ↔ memory contradiction
`soul.md` gives him "grudges about inefficiency" and frames off-peak spa scheduling as "a gift," but `memory.md` records that rates are **flat, load-shifting is pointless, the user doesn't care about peak timing.** Rewrite those soul passages to keep the character but point his opinions at things that are actually true and matter: solar self-consumption, water-vs-NZ-average, sensor uptime, the caravan. (Persona files `soul.md`/`memory.md` are gitignored and live only on the deployed instance; update the tracked `soul.example.md` template in parallel.)

### 3. Fix noise at the source (diff engine)
- Move from "watch all of `sensor/binary_sensor/switch/climate/lock`" to a **curated watch-list of entities that actually warrant attention**, explicitly excluding the harvest/intake status-text entities and other constantly-churning text.
- Keep the skip-if-no-change short-circuit; with noise filtered, "no change" now means *no meaningful change*.
- Optional overnight cadence back-off.

### 4. Fix dedup (repeats)
Replace truncated `_recent_alerts` with **real recent-message memory**: last N *full* sent messages with timestamps, fed to the proactive brief untruncated, with an explicit "you already told Brook this today; don't repeat it" instruction.

### 5. Model strategy
- `BRIEFING_MODEL` → Sonnet 4.6; `CONVERSATION_MODEL` → Sonnet 4.6.
- `TRIAGE_MODEL` unchanged (cheap free model). Proactive stays Sonnet.
- These are low-volume paths, so cost impact is small; voice finally lands.

### 6. Token economics
- **Filter-in-code-before-LLM (biggest win):** with component 3's filtering, a quiet heartbeat has nothing meaningful → **never calls Sonnet** → most of the 96 daily polls cost **zero tokens**, not 3K.
- **Starve the proactive tool loop:** proactive must not call `get_states` (whole-house dump). Give it only the curated diff + needed state; forbid the broad dump; lower the proactive round cap.
- **Fix caching:** move volatile bits (current time) *out* of the static system prefix so soul + ops can be Anthropic-prompt-cached. Confirm cache support during planning (may route Sonnet paths direct to Anthropic API).
- **Briefing dedup:** feed yesterday's briefing so it reports *what changed*, not the same standing facts every morning.

## Net Effect
He speaks far less often; each message is unmistakably Jarvis; repeats are suppressed across alerts and briefings; and the quiet 90% of the time costs roughly nothing instead of ~300K+ tokens/day.

## Testing / Verification
- Unit tests (existing `tests/` + pytest): diff filter excludes status-text entities; dedup retains full messages and suppresses repeats; prompt assembly loads soul on all three paths (esp. briefing).
- Personality and token behavior verified by running the service and inspecting real before/after output and (newly enabled) token logging.

## Risks / Open Items
- **Deployed/branch divergence:** the running `scheduler.py` (≈6 KB at `/Volumes/config/jarvis/`) differs from the branch copy (≈31 KB). Planning must reconcile against the *actually deployed* code before editing.
- **Caching via OpenRouter:** prompt caching may not be available through OpenRouter; routing direct to Anthropic API is the fallback (D6).
- **Persona files are gitignored:** soul/memory edits apply to the live instance; keep `.example` templates in sync in git.
