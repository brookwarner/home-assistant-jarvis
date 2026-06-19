# Jarvis Phase 1 — API-path cost relief Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut Jarvis's Anthropic API cost from ~$40–50/mo toward <$10/mo without changing its architecture — by prompt-caching the large static prefix, giving the 24/7 proactive poll an overnight quiet window, and moving routine traffic to direct-Anthropic Haiku.

**Architecture:** Jarvis routes all LLM calls through `litellm.acompletion`. We (1) add a shared helper that marks the system prompt with Anthropic `cache_control` so the ~6,800-token prefix (tools + system) is billed at ~0.1× on repeat calls; (2) add a configurable overnight quiet window to `scheduler.insight_poll`; (3) flip deployment model config to direct-Anthropic Haiku for proactive. This is the first of four phases (see the design spec); the later Agent-SDK/subscription phases are out of scope here.

**Tech Stack:** Python 3.12, litellm, APScheduler, pytest (`unittest.mock.patch`/`AsyncMock`), Home Assistant add-on.

**Reference spec:** `docs/superpowers/specs/2026-06-20-jarvis-subscription-brain-design.md`

---

## File Structure

- `router.py` — add `build_cached_messages(messages, model)` helper; apply it in `complete()`. Single home for the caching logic (imported elsewhere).
- `agents/conversation.py` — apply `build_cached_messages` in `_run_with_tools` and `_run_opus`; add the quiet-window model downgrade hook.
- `scheduler.py` — add quiet-window gating to `insight_poll` (skip the expensive call, or force the cheap model, inside the configured hours).
- `config.py` — add `PROACTIVE_QUIET_START` / `PROACTIVE_QUIET_END` config knobs.
- `addon/config.yaml` — surface the two quiet-window options in the add-on Configuration UI.
- `tests/test_router.py`, `tests/test_conversation.py`, `tests/test_scheduler.py`, `tests/test_config.py` — tests.
- Deployment note (no code): set direct-Anthropic Haiku models in the box `.env`.

---

## Task 1: Prompt-caching helper in router.py

**Files:**
- Modify: `router.py` (add helper near top, after imports)
- Test: `tests/test_router.py`

**Context:** litellm passes Anthropic prompt caching through when a message's `content` is a list of content blocks and a block carries `"cache_control": {"type": "ephemeral"}`. Marking the **system** block caches the whole prefix before it (tools render first in Anthropic order, so tools + system cache together under one breakpoint). Only do this for `anthropic/` models — OpenRouter passthrough caching is unreliable, and the design standardises on direct Anthropic.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_router.py`:

```python
from jarvis.router import build_cached_messages


def test_build_cached_messages_marks_system_for_anthropic():
    msgs = [
        {"role": "system", "content": "BIG STATIC PROMPT"},
        {"role": "user", "content": "hi"},
    ]
    out = build_cached_messages(msgs, "anthropic/claude-haiku-4-5")
    # system content becomes a content-block list with a cache_control marker
    assert out[0]["content"] == [
        {"type": "text", "text": "BIG STATIC PROMPT",
         "cache_control": {"type": "ephemeral"}}
    ]
    # user message is untouched
    assert out[1] == {"role": "user", "content": "hi"}
    # original list is not mutated
    assert msgs[0]["content"] == "BIG STATIC PROMPT"


def test_build_cached_messages_noop_for_non_anthropic():
    msgs = [{"role": "system", "content": "X"}, {"role": "user", "content": "y"}]
    out = build_cached_messages(msgs, "openrouter/anthropic/claude-4.6-sonnet")
    assert out == msgs


def test_build_cached_messages_no_system_is_noop():
    msgs = [{"role": "user", "content": "y"}]
    out = build_cached_messages(msgs, "anthropic/claude-haiku-4-5")
    assert out == msgs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_router.py -k build_cached_messages -v`
Expected: FAIL — `ImportError: cannot import name 'build_cached_messages'`

- [ ] **Step 3: Write minimal implementation**

In `router.py`, after the existing imports and before `_get_model`, add:

```python
def build_cached_messages(messages: list[dict], model: str) -> list[dict]:
    """Return a copy of ``messages`` with Anthropic prompt caching enabled on the
    system prompt. Marking the system block caches the whole prefix before it
    (tools render first, so tools + system cache under one breakpoint), billed at
    ~0.1x on repeat calls. No-op for non-Anthropic models (OpenRouter passthrough
    caching is unreliable) and when there is no system message."""
    if not model.startswith("anthropic/"):
        return messages
    out: list[dict] = []
    marked = False
    for m in messages:
        if not marked and m.get("role") == "system" and isinstance(m.get("content"), str):
            out.append({
                "role": "system",
                "content": [{
                    "type": "text",
                    "text": m["content"],
                    "cache_control": {"type": "ephemeral"},
                }],
            })
            marked = True
        else:
            out.append(m)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_router.py -k build_cached_messages -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: add Anthropic prompt-caching helper for the system prefix

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Apply caching in router.complete()

**Files:**
- Modify: `router.py` — `complete()` (currently passes `messages=messages` at line ~59)
- Test: `tests/test_router.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_router.py`:

```python
async def test_complete_passes_cached_messages_for_anthropic(mock_env, monkeypatch):
    monkeypatch.setenv("BRIEFING_MODEL", "anthropic/claude-haiku-4-5")
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(message=MagicMock(content="ok"))]
    from jarvis import router
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_ac:
        await router.complete("briefing", [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
        ])
    sent = mock_ac.call_args.kwargs["messages"]
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
```

(`mock_env` already sets `ANTHROPIC_API_KEY`; this is an `async def` test like the others in the file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_router.py -k cached_messages_for_anthropic -v`
Expected: FAIL — `sent[0]["content"]` is the raw string `"S"`, not a list (TypeError/KeyError on indexing).

- [ ] **Step 3: Write minimal implementation**

In `router.py` `complete()`, replace the `response = await litellm.acompletion(` block's `messages=messages,` argument by first building cached messages. Change:

```python
    response = await litellm.acompletion(
        model=model,
        messages=messages,
```

to:

```python
    response = await litellm.acompletion(
        model=model,
        messages=build_cached_messages(messages, model),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_router.py -v`
Expected: PASS (all router tests)

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "feat: cache the system prefix in router.complete (briefing/triage)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Apply caching in the conversation tool loop

**Files:**
- Modify: `agents/conversation.py` — `_run_with_tools` (system message built at line ~727; `acompletion` at ~740, ~779, ~795) and `_run_opus` (msgs at ~821; `acompletion` at ~839, ~865)
- Test: `tests/test_conversation.py`

**Context:** `_run_with_tools` builds `msgs = [{"role": "system", ...}] + messages` once, then loops calling `litellm.acompletion(messages=msgs, ...)`. Wrap `msgs` through the caching helper at each `acompletion` call. The user message carries the volatile `(now: …)` timestamp, so the system block stays byte-stable and caches across the loop.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_conversation.py`:

```python
async def test_run_with_tools_caches_system_prefix(mock_env, monkeypatch):
    monkeypatch.setenv("CONVERSATION_MODEL", "anthropic/claude-haiku-4-5")
    mock_ha = MagicMock(spec=HAClient)
    agent = ConversationAgent(mock_ha)
    agent._model = "anthropic/claude-haiku-4-5"

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock(
        finish_reason="stop",
        message=MagicMock(content="done", tool_calls=None),
    )]
    with patch("litellm.acompletion", new_callable=AsyncMock, return_value=mock_resp) as mock_ac:
        await agent._run_with_tools([{"role": "user", "content": "hi"}])

    sent = mock_ac.call_args.kwargs["messages"]
    assert sent[0]["role"] == "system"
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_conversation.py -k caches_system_prefix -v`
Expected: FAIL — `sent[0]["content"]` is a raw string, indexing `[0]["cache_control"]` raises TypeError.

- [ ] **Step 3: Write minimal implementation**

In `agents/conversation.py`:

1. Ensure the helper is imported. Near the existing router import, add (or extend) :

```python
from jarvis.router import build_cached_messages
```

2. In `_run_with_tools`, the three `litellm.acompletion(... messages=msgs ...)` / `messages=msgs` calls (the main loop call ~740, the empty-content retry ~779, and the max-rounds final call ~795) must send cached messages. Replace each `messages=msgs,` in those `acompletion` calls with:

```python
                messages=build_cached_messages(msgs, active_model),
```

3. In `_run_opus`, replace each `messages=msgs,` in its two `acompletion` calls (~839 and ~865) with:

```python
                messages=build_cached_messages(msgs, config.OPUS_MODEL),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_conversation.py -v`
Expected: PASS (all conversation tests, including the existing ones — caching is a no-op for the non-anthropic models used in other tests)

- [ ] **Step 5: Commit**

```bash
git add agents/conversation.py tests/test_conversation.py
git commit -m "feat: cache the system+tools prefix in the conversation/opus loops

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Quiet-window config knobs

**Files:**
- Modify: `config.py` (after `POLL_INTERVAL_MIN`, line ~44)
- Test: `tests/test_config.py`

**Context:** Two integer hours (0–23) bounding an overnight window during which the proactive poll skips the expensive model. `START=23, END=6` means 23:00–05:59 local is quiet. `START==END` (e.g. both 0) disables the window. Wrap-around (start > end) is the normal overnight case.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_quiet_window_defaults(monkeypatch):
    for k, v in {
        "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1", "HA_TOKEN": "h",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("PROACTIVE_QUIET_START", raising=False)
    monkeypatch.delenv("PROACTIVE_QUIET_END", raising=False)
    import importlib
    from jarvis import config as cfgmod
    importlib.reload(cfgmod)
    assert cfgmod.config.PROACTIVE_QUIET_START == 23
    assert cfgmod.config.PROACTIVE_QUIET_END == 6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -k quiet_window -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'PROACTIVE_QUIET_START'`

- [ ] **Step 3: Write minimal implementation**

In `config.py`, after the `POLL_INTERVAL_MIN` line, add:

```python
    # Overnight quiet window: during these local hours the proactive poll skips the
    # expensive model entirely (no unprompted nighttime notifications, no spend).
    # 0-23. START==END disables the window. START>END means an overnight wrap.
    PROACTIVE_QUIET_START: int = int(os.environ.get("PROACTIVE_QUIET_START", "23"))
    PROACTIVE_QUIET_END: int = int(os.environ.get("PROACTIVE_QUIET_END", "6"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -k quiet_window -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add proactive quiet-window config knobs

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: Quiet-window gating in insight_poll

**Files:**
- Modify: `scheduler.py` — add a module-level `_in_quiet_window(hour)` helper and call it inside `insight_poll` (before the diff/model call, after `resolve_mode`, ~line 360)
- Test: `tests/test_scheduler.py`

**Context:** `insight_poll` already computes a diff and calls `triage_agent_fn(diff_text)` only when there are changes. We add an earlier guard: if the current local hour is inside the quiet window, skip the model call entirely (still update the snapshot so we don't get a flood at wake-up). Use the same local-time source the rest of the scheduler uses (system local time; the add-on sets `TZ`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_scheduler.py`:

```python
from jarvis.scheduler import _in_quiet_window


def test_in_quiet_window_overnight_wrap(monkeypatch):
    monkeypatch.setattr("jarvis.config.config.PROACTIVE_QUIET_START", 23)
    monkeypatch.setattr("jarvis.config.config.PROACTIVE_QUIET_END", 6)
    assert _in_quiet_window(23) is True
    assert _in_quiet_window(2) is True
    assert _in_quiet_window(5) is True
    assert _in_quiet_window(6) is False
    assert _in_quiet_window(14) is False


def test_in_quiet_window_disabled_when_equal(monkeypatch):
    monkeypatch.setattr("jarvis.config.config.PROACTIVE_QUIET_START", 0)
    monkeypatch.setattr("jarvis.config.config.PROACTIVE_QUIET_END", 0)
    assert _in_quiet_window(0) is False
    assert _in_quiet_window(3) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler.py -k in_quiet_window -v`
Expected: FAIL — `ImportError: cannot import name '_in_quiet_window'`

- [ ] **Step 3: Write minimal implementation**

In `scheduler.py`, add a module-level helper (near the other module-level helpers like `_mode_poll_min`):

```python
def _in_quiet_window(hour: int) -> bool:
    """True if the given local hour is inside the proactive quiet window.
    START==END disables the window; START>END is an overnight wrap."""
    from jarvis.config import config
    start, end = config.PROACTIVE_QUIET_START, config.PROACTIVE_QUIET_END
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # overnight wrap
```

Then in `insight_poll`, immediately after `_last_proactive_run = now` (line ~364) and before `states = await ha_client.get_states()`, add:

```python
            import datetime as _dt
            if _in_quiet_window(_dt.datetime.now().hour):
                # Refresh the snapshot so we don't dump a backlog at wake-up, then skip.
                states = await ha_client.get_states()
                group_members = await _get_watch_group_members(ha_client)
                watched_states = [
                    s for s in states
                    if _is_watched_in_mode(s.get("entity_id", ""), mode)
                    or s.get("entity_id", "") in group_members
                ]
                global _last_snapshot
                _last_snapshot, _ = compute_state_diff(watched_states, _last_snapshot)
                logger.debug("insight_poll: quiet window, snapshot refreshed, no model call")
                return
```

Note: `_last_snapshot` is already declared `global` at the top of `insight_poll` (line ~354), so the inner `global` line is redundant if present there — if Python raises `SyntaxError: name '_last_snapshot' is assigned to before global declaration`, remove the inner `global _last_snapshot` line since the function already declares it.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scheduler.py -k in_quiet_window -v`
Expected: PASS

- [ ] **Step 5: Run the full scheduler suite (no regressions)**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: skip the proactive model call during the overnight quiet window

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: Surface quiet-window options in the add-on UI

**Files:**
- Modify: `addon/config.yaml` (options + schema blocks)

**Context:** The add-on exposes tunables in the Configuration screen and passes them as env vars to the container. Add the two quiet-window hours alongside the existing `proactive_poll_minutes`/`proactive_enabled` options. (No test — this is add-on metadata; validated by the Supervisor on rebuild.)

- [ ] **Step 1: Add the options**

In `addon/config.yaml`, under the `options:` map (near `proactive_poll_minutes`), add:

```yaml
  proactive_quiet_start: 23
  proactive_quiet_end: 6
```

And under the `schema:` map, add:

```yaml
  proactive_quiet_start: int(0,23)
  proactive_quiet_end: int(0,23)
```

- [ ] **Step 2: Verify run.sh exports them (or document the gap)**

Run: `grep -n "proactive_poll_minutes\|PROACTIVE_POLL\|bashio" addon/run.sh`
Expected: shows how existing proactive options are read from the add-on config and exported as env vars. Mirror that pattern for `proactive_quiet_start` → `PROACTIVE_QUIET_START` and `proactive_quiet_end` → `PROACTIVE_QUIET_END`. If `run.sh` reads options generically, no change is needed; if each is explicit, add the two exports.

- [ ] **Step 3: Commit**

```bash
git add addon/config.yaml addon/run.sh
git commit -m "feat: expose proactive quiet-window hours in the add-on config

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: Deployment config — direct-Anthropic Haiku for routine traffic

**Files:** none (operational change on the HAOS box; documented here and in the README)

**Context:** Caching is reliable only on `anthropic/` models, and Haiku is ~3× cheaper than Sonnet. The box `.env` currently overrides the proactive/conversation models to a `openrouter/...claude-4.6-sonnet` string. This task switches routine traffic to direct-Anthropic Haiku and confirms caching takes effect. This is the single biggest immediate cost lever and needs no code.

- [ ] **Step 1: Inspect the live model config**

```bash
ssh haos 'grep -E "MODEL|OPENROUTER|ANTHROPIC_API_KEY" /config/jarvis/.env'
```

Expected: see which models are overridden (likely `CONVERSATION_MODEL` / `PROACTIVE_MODEL` set to an `openrouter/...sonnet` string), and confirm `ANTHROPIC_API_KEY` is present.

- [ ] **Step 2: Set routine models to direct-Anthropic Haiku**

Edit `/config/jarvis/.env` on the box so:
- `PROACTIVE_MODEL=anthropic/claude-haiku-4-5`
- `TRIAGE_MODEL=anthropic/claude-haiku-4-5`
- `BRIEFING_MODEL=anthropic/claude-haiku-4-5`
- `CONVERSATION_MODEL=anthropic/claude-haiku-4-5` (interactive chat; revisit to Sonnet only if quality drops)

Keep `OPUS_MODEL` for the delegate path. Leave `OPENROUTER_API_KEY` unset/empty so the `anthropic/` branch in `router.py` is taken.

- [ ] **Step 3: Restart the add-on and verify**

```bash
ssh haos 'ha addons restart local_jarvis'
```

Then after a few proactive cycles:

```bash
ssh haos 'ha addons logs local_jarvis --lines 100000 | grep "cost agent=" | tail -10'
```

Expected: model strings now read `anthropic/claude-haiku-4-5`. Confirm caching by checking that input token counts on repeat conversation calls drop (litellm logs `cache_read_input_tokens` when caching hits; if `usage.py` doesn't surface it, this is picked up in Task 8).

- [ ] **Step 4: Commit the README/docs note**

Update `README.md` (or the deploy section) to state: routine agents run on direct-Anthropic Haiku with prompt caching; set `OPENROUTER_API_KEY` empty for caching to apply.

```bash
git add README.md
git commit -m "docs: note direct-Anthropic Haiku + caching as the default cost posture

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: Surface cache hits in usage logging

**Files:**
- Modify: `usage.py` — `log_completion` (token extraction ~line 28–42, log line ~45)
- Test: `tests/` (add `tests/test_usage.py`)

**Context:** Today `usage.py` logs `in`/`out`/`cost` but not cache reads, so we can't see caching working. litellm surfaces cache token counts on `response.usage` as `cache_read_input_tokens` / `cache_creation_input_tokens` (Anthropic). Add them to the running totals and the log line so the Phase-1 win is observable.

- [ ] **Step 1: Write the failing test**

Create `tests/test_usage.py`:

```python
from unittest.mock import MagicMock
from jarvis import usage


def test_log_completion_records_cache_reads(caplog):
    resp = MagicMock()
    resp.usage = MagicMock(
        prompt_tokens=7000, completion_tokens=40,
        cache_read_input_tokens=6500, cache_creation_input_tokens=0,
    )
    resp._hidden_params = {"response_cost": 0.001}
    resp.model = "anthropic/claude-haiku-4-5"
    import logging
    with caplog.at_level(logging.INFO, logger="jarvis.usage"):
        usage.log_completion(resp, "conversation")
    assert "cache_read=6500" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_usage.py -v`
Expected: FAIL — log line has no `cache_read=` field.

- [ ] **Step 3: Write minimal implementation**

In `usage.py` `log_completion`, after the existing `out_tok` extraction, add:

```python
        cache_read = getattr(usage, "cache_read_input_tokens", None) if usage else None
        cache_write = getattr(usage, "cache_creation_input_tokens", None) if usage else None
```

Extend the `logger.info(...)` format string and args to include `cache_read=%s`:

```python
        logger.info(
            "cost agent=%s model=%s in=%s out=%s cache_read=%s cost=%s | session: %d reqs $%.4f",
            agent, model, in_tok, out_tok, cache_read, cost_str,
            int(_totals["requests"]), _totals["cost"],
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_usage.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `pytest -q`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add usage.py tests/test_usage.py
git commit -m "feat: log cache_read tokens so prompt caching is observable

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Done criteria

- `pytest -q` green.
- Add-on rebuilt/restarted on the box; `cost agent=` log lines show `anthropic/claude-haiku-4-5` and `cache_read=` climbing on repeat calls.
- No proactive notifications fire between `PROACTIVE_QUIET_START` and `PROACTIVE_QUIET_END`.
- Next-day Anthropic Console daily bar drops materially vs the ~$1.5–3.2/day baseline.

## Out of scope (later phases — see spec)

- Claude Agent SDK / subscription brain (`brain.py`, `LLM_BACKEND` switch, one-time `claude login`, tool porting). Phases 2–4.
