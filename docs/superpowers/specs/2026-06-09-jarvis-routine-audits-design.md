# Jarvis Routine Audits — Design

**Date:** 2026-06-09
**Status:** Design approved, ready for implementation plan

## Purpose

Jarvis should maintain himself. Periodically inspect the Home Assistant config for
maintenance problems and surface them *before* they become failures. This is motivated
by three issues found by hand on 2026-06-09 that a routine audit would have caught
automatically:

1. **Dead watchdog** — an automation/entity silently stopped working (stale / unavailable).
2. **Broken `shell_command` path** — a command pointing at a script path that no longer resolves.
3. **Soul↔memory contradiction** — persona docs contradicting stored memory (deferred to v2; see Scope).

## Scope (v1)

Three **deterministic** checks, run **weekly**, **report-only** (no auto-fixing), delivered to
**Telegram + an HA persistent notification**.

| In scope (v1) | Out of scope |
| --- | --- |
| Dead/unavailable entities | Doc/memory contradiction check (v2 — fuzzy, needs LLM judgment) |
| Broken service/path refs (`entity_id`, `service`, `shell_command` paths) | Auto-fixing / self-editing config (separate card: dry-run simulation) |
| Stale automations (never/long-since triggered) | Conflicting-controls detection (later) |
| | Noisy-notification detection (later) |
| | Auto-filing findings as issue-tracker cards |

## Approach

**Hybrid, leaning deterministic (Approach C).** Detection is pure Python and is the single
source of truth — it cannot invent findings. A thin LLM pass only *phrases* the
already-found list into Jarvis's voice for the Telegram message. The deterministic findings
are also the seam where the v2 doc/memory check will later attach.

Rejected alternatives:
- **Pure deterministic (A):** maximally reliable but mechanical-sounding summary; chosen detection model is identical, only the narration differs.
- **LLM agent-driven (B):** can hallucinate or miss findings — unacceptable for a tool whose entire value is *trustworthy* signal.

## Architecture & components

Lives in the live add-on tree `/config/jarvis/`. No new infrastructure.

```
scheduler.py
  └─ add_job(run_audit, "cron", day_of_week="sun", hour=8, id="weekly_audit")
       └─ audit.run_audit(ha_client) ───────────────┐
                                                     ▼
agents/audit.py
  ├─ check_dead_entities(states, ignore)              → [Finding]  ┐ deterministic
  ├─ check_broken_refs(yaml_files, states, services)  → [Finding]  ├ source of truth
  ├─ check_stale_automations(states, cfg)             → [Finding]  ┘
  ├─ load_state()/save_state()  → audit_state.json    (NEW vs ONGOING diffing)
  ├─ load_ignore()              → audit_ignore.json   (user-acknowledged false positives)
  ├─ summarize(findings)        → thin LLM pass → Jarvis-voice text (fallback: template)
  └─ deliver(text)              → Telegram send_message + HA persistent_notification
```

### Finding (data shape)

A small dataclass, one uniform shape consumed by diffing, formatting, and the LLM pass:

```python
@dataclass
class Finding:
    check: str            # "dead_entity" | "broken_ref" | "stale_automation"
    key: str              # stable dedup/diff key, e.g. "dead_entity:sensor.foo"
    severity: str         # "info" | "warn" | "high" (scales with age)
    ref: str              # entity_id / service / file path the finding is about
    detail: str           # human-readable explanation
    first_seen: str       # ISO date, carried from audit_state.json across runs
```

`first_seen` is populated from `audit_state.json` so the report can age findings; brand-new
keys get the current run's date.

### Boundary

- **Detection = pure Python**, offline-testable with fixture states/YAML. The LLM is handed
  the finished findings list and **cannot add, drop, or invent** findings.
- Two JSON sidecars next to the module: `audit_state.json` (last run's keys + `first_seen`)
  and `audit_ignore.json` (user-maintained keys/globs to never flag; seeded empty).

## The three checks

All checks run conservatively — **favour false negatives over false positives**. A maintenance
tool that cries wolf gets muted.

### 1. Dead/unavailable entities
From `/api/states`: flag entities currently `unavailable`/`unknown` whose `last_changed` is
≥ **24h** ago (so a momentarily-rebooting device doesn't trip it), excluding `audit_ignore.json`.
Severity scales with age (weeks-dead → higher). Reports entity id, friendly name, and dead-duration.

### 2. Broken service/path refs
Static parse of `automations.yaml`, `scripts/*.yaml`, `configuration.yaml`, cross-referenced
against live state. **Only literal refs** are flagged; anything templated / `!secret` / Jinja
is skipped to stay high-confidence:
- Literal `entity_id:` values **absent from `/api/states`** → likely renamed/deleted entity.
- `service:` / `action:` calls whose `domain.service` isn't in `/api/services` → dead service ref.
- `shell_command:` definitions whose command invokes a **script file path that does not exist on
  disk** → the exact class that bit us on 2026-06-09.

### 3. Stale automations
From automation entities' `last_triggered` attribute: flag **never-triggered** automations and
those with `last_triggered` older than **90 days**, minus the ignore list. Overlap with check 2's
"automation references a missing entity" is deduped by `key`.

### Noise control (shared)
- **NEW vs ONGOING:** each finding's `key` is matched against `audit_state.json`. Report marks
  each `🆕 NEW` or `↻ ongoing (N weeks)`. Known-but-unfixed items stop shouting after week 1 but
  remain tallied.
- **Ignore list:** `audit_ignore.json` is a user-maintained array of keys/globs the audit never
  flags — the escape hatch for legitimate-forever cases. Seeded empty with an explanatory comment.
- **All-clear:** zero non-ignored findings → a one-line "Audit clean — N entities, M automations
  checked" message (not silence), so it's clear the audit ran.

Thresholds (`24h`, `90 days`) are module constants for v1; easy to promote to add-on options later.

## Scheduling & trigger
- New APScheduler job in `build_scheduler()`: `cron, day_of_week="sun", hour=8, id="weekly_audit"`
  — after the 07:30 morning briefing, before the existing Sunday 09:00 water summary (no collision).
- On-demand path: a `run_audit` tool/intent in `conversation.py` calling the same
  `audit.run_audit()`, so "run an audit" over Telegram and the cron share one code path.

## Reporting
- `summarize(findings)` builds a structured block (grouped by check, NEW first, severity-sorted),
  hands it to the thin LLM pass for Jarvis-voice phrasing, then delivers to **both** sinks:
  - Telegram via the existing `send_message` pathway.
  - HA `persistent_notification.create` with stable `notification_id="jarvis_weekly_audit"` so each
    week **replaces** the prior notification rather than stacking.
- LLM-pass guardrail: instructed to narrate only the supplied findings; if the LLM call fails, fall
  back to deterministic template text (findings still delivered). **Detection never depends on the LLM.**

## Error handling
- Each check wrapped so one failure (e.g. malformed YAML) degrades to a logged warning + a
  "⚠️ couldn't run check X" line, not a dead audit.
- HA API / file errors caught; total failure logs and sends a short "audit failed to run" notice —
  so a silently-broken auditor doesn't itself become an undetected dead watchdog (dogfooding).
- `audit_state.json` / `audit_ignore.json`: missing or corrupt → treated as empty and recreated.

## Testing
- Pure-Python checks unit-tested in `tests/` against fixtures: a frozen `/api/states` JSON snapshot
  and sample YAML with planted issues (deleted entity ref, missing shell-script path, 200-day-stale
  automation, 24h+ unavailable entity). Assert exact findings.
- NEW-vs-ONGOING diffing tested by running twice over two state snapshots.
- LLM summary pass: assert it's called with the right findings and that the template fallback fires
  when it raises — **wording is not asserted**.

## Deployment
Python-only change to the live tree (`/config/jarvis/agents/audit.py` + `scheduler.py` +
`conversation.py` + tests). Per deploy notes: edit under `/config/jarvis/`, then
`ha apps restart local_jarvis` (no rebuild — code is read from mapped `/config`).

## Future (v2+)
- Doc/memory contradiction check (LLM-judged), attaching at the `summarize` seam.
- Conflicting-controls and noisy-notification detection.
- Promote thresholds to add-on options.
