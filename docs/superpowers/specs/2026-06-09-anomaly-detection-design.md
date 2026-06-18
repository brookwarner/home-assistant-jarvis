# Daily Anomaly Detection (v1) — Design

**Date:** 2026-06-09
**Status:** Approved
**Branch:** `claude/anomaly-detection` (isolated worktree)

## Goal

Detect and explain meaningful deviations in daily home metrics versus a learned baseline, surfaced (low-noise) in the morning briefing in Jarvis's voice. v1 covers **numeric daily-baseline anomalies** only.

## Scope (locked)

- **In:** numeric daily-total/average anomalies from HA long-term statistics (water, energy, power, temperatures, …). Both directions (spike *and* unusual drop).
- **Out (deferred):** per-device runtime/duration anomalies; live rule-based state combinations (e.g. windows-open-during-HVAC — already partly served by the proactive watch-list).

## Coordination constraints (multi-agent)

Minimise edits to hot shared files. New logic in a **new module `anomaly.py`**. Do **not** touch memory/, the rec-engine, or the audit code. Anomaly tuning read from **env vars with defaults inside `anomaly.py`** (no `config.py`/`addon` edits in v1; those come in coordinated integration). Work on a feature branch off `main`; **no force-push to main**; **deployment held** for coordinated integration.

## Pipeline (daily, inside the morning-briefing job)

1. **Discover** — all `statistic_id`s with long-term statistics, via a new additive `ha_client.list_statistic_ids()` (read-only DB query, mirrors `get_statistics`' connection).
2. **Baseline + detect** — per statistic, pull ~31 days of daily usage (`get_statistics(period="day")`). Baseline = **median + MAD** over complete days excluding the latest. Score the latest complete day: robust z = `0.6745 * (yesterday - median) / MAD`.
   - Flag when `abs(z) >= ANOMALY_Z` (default 3.5) **and** `abs(yesterday - median) >= ANOMALY_MIN_ABS` (default 1.0) **and** `abs(pct) >= ANOMALY_MIN_PCT` (default 0.25).
   - Guards: `MAD == 0` (constant signal) → skip; fewer than `ANOMALY_MIN_DAYS` (default 14) baseline days → skip; non-finite → skip.
   - Severity from `abs(z)`: `>=6` high, `>=4` medium, else low.
3. **Surface filter** — keep an anomaly if its `statistic_id` matches a curated surface-list substring (`ANOMALY_SURFACE`, default: `water,energy,power,spa,heater,gas`) **OR** severity == high. ("Auto-discover, curated surfacing.")
4. **Explain** — surfaced anomalies become short human descriptors (e.g. `"Water: 480 L yesterday vs ~150 L typical (+330 L, 3.2x)"`) handed to the briefing, which explains them in voice.

## Public interface (`anomaly.py`)

- `compute_baseline(values: list[float]) -> dict | None` — `{median, mad, n}` or None if insufficient.
- `score_day(value, baseline) -> dict` — `{z, pct, severity}`.
- `async detect(ha_client) -> list[Anomaly]` — discover → baseline → score → flag.
- `surface(anomalies) -> list[Anomaly]` — apply the surface filter.
- `async detect_and_surface(ha_client) -> list[str]` — convenience: returns ready-to-inject descriptor strings (empty list on any failure).

`Anomaly` is a small dataclass/dict: `statistic_id, name, unit, yesterday, median, z, pct, severity, descriptor`.

## Integration footprint

- **New:** `anomaly.py`, `tests/test_anomaly.py`.
- **Edit (small):** `ha_client.py` (+`list_statistic_ids`, appended); `scheduler.py` `morning_briefing` (call `detect_and_surface`, pass to briefing); `agents/briefing.py` `generate(summary, anomalies: list[str] | None = None)` (+ a prompt section: "If anomalies are listed, lead with/include them, explained in your voice; otherwise omit").
- **Untouched:** `config.py`, `conversation.py`, `addon/`, `bot.py`, memory, rec-engine, audit code.

## Error handling

Any DB/stat failure, thin history, or bad data → that signal is skipped silently. `detect_and_surface` returns `[]` on any top-level failure; the briefing always generates with or without anomalies.

## Testing (`tests/test_anomaly.py`)

- `compute_baseline`: correct median/MAD; None below `ANOMALY_MIN_DAYS`.
- `score_day`: z/pct/severity bands.
- `detect`: flags a synthetic spike and an unusual drop; ignores in-noise; respects min-abs/min-pct floors; handles MAD=0 and short history (mocked `ha_client.list_statistic_ids` + `get_statistics`).
- `surface`: keeps allow-list matches and high-severity; drops low-severity non-listed.
- `detect_and_surface`: returns descriptor strings; `[]` when the client raises.

## Deploy

Held. When integration is coordinated: merge the branch, then (optionally) promote anomaly tuning to add-on options, then deploy via the add-on update flow. v1 needs no new HA helper.
