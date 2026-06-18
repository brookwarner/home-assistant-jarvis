# Jarvis Brief Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Jarvis speak less, sound like himself when he does, stop repeating himself, and stop burning tokens — by refactoring the agent brief and fixing the upstream noise/dedup/token causes.

**Architecture:** Local Python service (`jarvis/`). Restructure the system prompt into voice/operational/mode layers; load the soul on every voice-bearing path (incl. briefing); filter status-text noise out of the proactive diff so quiet polls never call the LLM; replace truncated dedup with full recent-message memory; promote voice paths to Sonnet; make the static prompt prefix cacheable and add token logging.

**Tech Stack:** Python 3.12, pytest, litellm (OpenRouter / Anthropic), python-telegram-bot, APScheduler.

**Base:** branch `claude/jarvis-brief-refactor` off `main` (deployed reality). The unmerged "recommendations engine" (`scheduler.py` 816-line rewrite on `claude/exciting-lehmann-c2b11c`) is explicitly NOT in scope.

**Spec:** `docs/superpowers/specs/2026-06-08-jarvis-brief-refactor-design.md`

**Key facts the executor MUST know:**
- The package imports as `jarvis` (e.g. `from jarvis.config import config`). The deployed layout is `/homeassistant/jarvis` with `PYTHONPATH=/homeassistant`. This worktree dir is `jarvis-brief-refactor`, so tests need a path shim (Task 0).
- `soul.md`, `memory.md`, and `.env` are **gitignored** — they live only on the deployed box. Tracked templates are `soul.example.md` / `ha_entities.example.md`. Edits to persona/model config must be applied to the live box separately (Task 9).
- All current-time strings are injected via `_now_str()`. Moving them out of the static system prefix is what makes prompt caching possible (Task 7).

---

### Task 0: Environment & green baseline

**Files:**
- Create: `conftest.py` (repo root — path shim so `import jarvis` works)
- Test: all of `tests/`

- [ ] **Step 1: Create venv and install deps**

```bash
cd /Volumes/config/jarvis/.claude/worktrees/jarvis-brief-refactor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pytest pytest-asyncio
```

- [ ] **Step 2: Add a path shim so the package imports as `jarvis`**

The worktree dir isn't named `jarvis`, so `from jarvis.config import config` fails. Create `conftest.py` at the worktree root:

```python
# conftest.py — make this worktree importable as the `jarvis` package
import sys
from pathlib import Path

_here = Path(__file__).resolve().parent           # .../jarvis-brief-refactor
_alias = _here.parent / "jarvis"                   # sibling name the package expects
# Expose the parent dir on sys.path and alias this dir as `jarvis`
sys.path.insert(0, str(_here.parent))
if not _alias.exists():
    import types, importlib.util
    pkg = types.ModuleType("jarvis")
    pkg.__path__ = [str(_here)]
    sys.modules["jarvis"] = pkg
```

- [ ] **Step 3: Run the baseline suite**

Run: `cd /Volumes/config/jarvis/.claude/worktrees/jarvis-brief-refactor && source .venv/bin/activate && python -m pytest tests/ -v`
Expected: collection succeeds. If any tests fail on `main` before changes, record which — do not fix unrelated failures; note them and proceed only with the user's OK (per using-git-worktrees baseline rule).

- [ ] **Step 4: Commit the shim**

```bash
git add conftest.py
git commit -m "test: add path shim so worktree imports as jarvis package"
```

---

### Task 1: Mode-aware system prompt (voice/operational/mode layers; drop terseness mandate)

Refactor `agents/conversation.py:_load_system_prompt()` (line 394) to take a `mode` argument and assemble three layers. Remove the voice-killing BREVITY mandate (line 410). Move the current-time line (398) out of the cached prefix — it goes into the per-call user message instead (wired in Task 7; for now, drop it from the static prompt and rely on the existing time line already present in the user-facing context).

**Files:**
- Modify: `agents/conversation.py` (`_load_system_prompt`, ~394–440; callers at `_run_with_tools` ~566 and `run_proactive`)
- Test: `tests/test_conversation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_conversation.py  (add)
def test_prompt_loads_soul_and_drops_terseness(monkeypatch, tmp_path):
    from jarvis.agents import conversation
    p = conversation._load_system_prompt(mode="conversation")
    # voice layer present (soul example phrase or fallback bot name)
    assert "smart home" in p.lower() or "home" in p.lower()
    # terseness mandate removed
    assert "First sentence is the answer" not in p
    # banned-filler rule retained
    assert "certainly" in p.lower()

def test_prompt_mode_layer_differs():
    from jarvis.agents import conversation
    proactive = conversation._load_system_prompt(mode="proactive")
    convo = conversation._load_system_prompt(mode="conversation")
    assert "PROACTIVE" in proactive
    assert "SILENT" in proactive
    assert proactive != convo
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/test_conversation.py -k "terseness or mode_layer" -v`
Expected: FAIL (TypeError: unexpected `mode`, or assertion failures).

- [ ] **Step 3: Implement the layered prompt**

Replace the body of `_load_system_prompt` (line 394) with:

```python
def _operational_layer() -> str:
    return (
        "You have tools to read entity states, control devices, remember things, and edit HA config files.\n"
        "To find entity IDs: use search_entities with a broad keyword. "
        "If search_entities returns nothing, try a different keyword, then try get_states_by_domain, then try get_state with a guessed ID. "
        "Never give up after one failed search — try at least 3 approaches.\n"
        "When taking actions, confirm what you did in one sentence.\n"
        "When asked questions, fetch live data — never guess entity IDs without trying.\n\n"
        f"TIMEZONE: All HA entity timestamps (last_changed, last_updated, etc.) are UTC. "
        f"Local timezone is {_tz()}. Always convert UTC to local time before reporting any time or date. "
        f"A HA timestamp of 21:00 UTC is not 9pm locally.\n\n"
        "FORMATTING: Never use markdown. No bold, italics, tables, * bullets, # headers, backticks.\n\n"
        "Never say 'certainly', 'of course', 'happy to help', 'great question'. Don't pad — but do not flatten "
        "your voice into a terse status report either. Speak as yourself.\n\n"
        "TELEGRAM TOOLS:\n"
        "send_message — pushes a message to the user immediately, mid-turn. "
        "If you used send_message to deliver the full answer, return empty string as your final text.\n"
        "ask_user — sends a question and blocks until the user replies. Use before irreversible actions. "
        "Do not use ask_user in proactive mode."
    )

_MODE_LAYERS = {
    "conversation": (
        "You are replying to a message from the user. Answer in your own voice."
    ),
    "proactive": (
        "PROACTIVE MODE: you were triggered by a home event or a scheduled poll, not a user message. "
        "Silence is the default. Only speak if this genuinely warrants interrupting the user. "
        "Use send_message to notify if warranted. If no notification is needed, include the word SILENT "
        "on its own line anywhere in your response; your reasoning will be logged but not sent. "
        "Do not repeat anything from 'Recent messages already sent' below."
    ),
}

def _load_system_prompt(mode: str = "conversation") -> str:
    """Assemble voice + operational + mode layers. Static across calls so it can be prompt-cached;
    volatile data (current time, recent messages) is injected via the user message, not here."""
    op = _operational_layer()
    mode_layer = _MODE_LAYERS.get(mode, _MODE_LAYERS["conversation"])

    memory = ""
    if MEMORY_PATH.exists():
        mem = MEMORY_PATH.read_text().strip()
        if mem:
            memory = f"\n\nYour persistent memory notes:\n{mem}"

    if SOUL_PATH.exists():
        from jarvis.config import config
        soul = SOUL_PATH.read_text()
        soul = soul.replace("{BOT_NAME}", config.BOT_NAME).replace("{OWNER_NAME}", config.OWNER_NAME)
        voice = soul
    else:
        voice = f"You are {_bot_name()}, an AI smart home assistant talking to {_owner_name()}."

    return f"{voice}\n\n---\n\n{op}\n\n---\n\n{mode_layer}{memory}"
```

- [ ] **Step 4: Update callers to pass mode**

In `_run_with_tools` (line ~566) change the system-prompt line to accept a mode param. Update its signature and the line that builds `msgs`:

```python
    async def _run_with_tools(self, messages: list[dict], model: str | None = None, mode: str = "conversation") -> str:
        active_model = model or self._model
        msgs = [{"role": "system", "content": _load_system_prompt(mode)}] + messages
```

In `run_proactive` (line ~551) pass `mode="proactive"`:

```python
            response_text = await self._run_with_tools(messages, model=model, mode="proactive")
```

- [ ] **Step 5: Run tests to verify pass**

Run: `python -m pytest tests/test_conversation.py -v`
Expected: PASS (new tests + existing ones; fix any existing test that asserted on the old BREVITY string).

- [ ] **Step 6: Commit**

```bash
git add agents/conversation.py tests/test_conversation.py
git commit -m "feat: layered mode-aware system prompt; drop terseness mandate"
```

---

### Task 2: Briefing loads the soul

`agents/briefing.py` currently uses `briefing_prompt.md` as the entire system prompt (line 20) — no soul. Make it reuse the conversation prompt's voice + a briefing mode-note.

**Files:**
- Modify: `agents/briefing.py` (`_load_system_prompt` line 20; `generate` line 26)
- Test: `tests/test_briefing.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_briefing.py (add)
def test_briefing_prompt_includes_voice(monkeypatch):
    from jarvis.agents import briefing
    p = briefing._load_system_prompt()
    # briefing must carry the persona, not just the terse standalone prompt
    assert "briefing" in p.lower()
    assert "no markdown" in p.lower() or "plain prose" in p.lower()
    # voice marker: soul (if present) or bot identity
    assert "home" in p.lower()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_briefing.py -k voice -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Replace `_load_system_prompt` in `agents/briefing.py` (line 20):

```python
def _load_system_prompt() -> str:
    # Reuse the conversation voice layer so the briefing sounds like Jarvis,
    # then append the briefing-specific shape.
    from jarvis.agents.conversation import _load_system_prompt as _voice_prompt
    base = _voice_prompt(mode="conversation")
    briefing_note = (
        "\n\n---\n\nBRIEFING MODE: Generate a morning briefing from current home state. "
        "Lead with the single most interesting or urgent thing. Under 150 words, plain prose, no markdown. "
        "Report what CHANGED since yesterday — do not re-list standing facts (water %, the same offline devices, "
        "the same backup time) that were already true in the previous briefing shown below. Don't invent data."
    )
    return base + briefing_note
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_briefing.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/briefing.py tests/test_briefing.py
git commit -m "feat: briefing loads the soul/voice layer instead of standalone prompt"
```

---

### Task 3: Reconcile soul ↔ memory contradiction (content edit)

`soul.example.md` (tracked template) and the deployed `soul.md` give Jarvis "grudges about inefficiency" and frame off-peak spa scheduling as "a gift", contradicting `memory.md` (flat rates, load-shifting pointless). Rewrite those passages to keep the character but aim it at true targets.

**Files:**
- Modify: `soul.example.md`
- (Deployed `soul.md` updated in Task 9 — gitignored.)

- [ ] **Step 1: Edit `soul.example.md`**

Remove/rewrite any passage that treats peak-vs-off-peak timing or load-shifting as mattering. Replace the "grudges about inefficiency / spa ran on-peak" theme with equally opinionated lines pointed at things that are actually true for this home: maximising solar self-consumption, water usage staying under the regional average, sensor uptime, outbuildings staying warm. Keep the theatrical voice; change only the targets. Concretely, replace the bullet that holds a grudge about on-peak spa runs with one about, e.g., a sensor that dropped out at the interesting moment, or water creeping toward the regional average.

- [ ] **Step 2: Sanity check — no peak/off-peak nagging remains**

Run: `grep -niE "off-peak|on-peak|peak rate|load.?shift|guilt-free" soul.example.md`
Expected: no lines that treat timing as financially meaningful (a neutral mention is fine; a grudge is not).

- [ ] **Step 3: Commit**

```bash
git add soul.example.md
git commit -m "style: retarget Jarvis's efficiency opinions at true levers (flat rates)"
```

---

### Task 4: Diff-engine noise filter (evidence-driven allow-list)

**Why this is the biggest token lever:** the live log shows `insight_poll: 9–19 changes detected` *every 15 minutes* — each non-empty diff wakes the Sonnet proactive model. The diff watches all of `WATCHED_DOMAINS` (`sensor/binary_sensor/switch/climate/lock`) with a 2.0-abs / 5% threshold, so ordinary drift in power, voltage, humidity, solar elevation, tide, vacuum, and printer sensors trips it constantly. The spec calls for moving to a **curated allow-list of entities that warrant attention**, not a blacklist chase.

**The diff contents are NOT currently logged, so the exact culprits are unknown.** This task therefore starts by instrumenting, then builds the allow-list from real data. Do not hardcode a guessed list.

**Files:**
- Modify: `scheduler.py` (constants near line 13; `compute_state_diff` line 34; `insight_poll` line 156)
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Instrument the diff so the real culprits are visible**

In `insight_poll` (scheduler.py ~line 167), change the count-only log to also emit the diff lines at DEBUG:

```python
            logger.info(f"insight_poll: {len(diff)} changes detected")
            logger.debug("insight_poll diff:\n" + "\n".join(diff))
```

- [ ] **Step 2: Capture one real cycle's diff** (execution-time, on the box)

Temporarily set `LOG_LEVEL=DEBUG`, restart, and capture a few cycles:

```bash
grep -A40 "insight_poll diff" /homeassistant/jarvis/jarvis.log | tail -120
```

Record the entity_ids that appear repeatedly with trivial changes (the noise) vs. the few that would actually matter (doors, locks, leak/moisture, smoke, specific power/temperature). This evidence defines the allow-list in Step 4. Revert `LOG_LEVEL` after.

- [ ] **Step 3: Write failing tests for an allow-list filter**

```python
# tests/test_scheduler.py (add)
def test_diff_only_includes_allowlisted_entities():
    from jarvis.scheduler import compute_state_diff
    last = {
        "binary_sensor.garage_door": "off",
        "sensor.living_room_voltage": "230.0",
    }
    states = [
        {"entity_id": "binary_sensor.garage_door", "state": "on"},      # allow-listed → kept
        {"entity_id": "sensor.living_room_voltage", "state": "245.0"},  # noise → dropped
    ]
    _, diff = compute_state_diff(states, last, domains=["binary_sensor", "sensor"])
    assert any("garage_door" in d for d in diff)
    assert not any("voltage" in d for d in diff)

def test_diff_allowlist_supports_patterns():
    from jarvis.scheduler import _is_watched
    assert _is_watched("lock.front_door")          # whole 'lock' domain watched
    assert _is_watched("binary_sensor.garage_door")
    assert not _is_watched("sensor.printer_uptime")
```

- [ ] **Step 4: Run to verify failure**

Run: `python -m pytest tests/test_scheduler.py -k "allowlist or only_includes" -v`
Expected: FAIL (`_is_watched` undefined).

- [ ] **Step 5: Implement the allow-list**

Add near line 13 in `scheduler.py` (seed the lists from Step 2 evidence; the values below are a safe starting set — security/safety entities plus explicitly chosen high-value sensors):

```python
import re

# Proactive attention is OPT-IN. Only these entities/domains can wake the model.
# Whole domains worth watching for state changes:
WATCHED_FULL_DOMAINS = {"lock"}
# Specific entities / id-substrings worth watching (extend from live-diff evidence):
WATCHED_ENTITY_SUBSTRINGS = (
    "garage_door", "front_door", "back_door",
    "moisture", "leak", "smoke", "water_sensor",
    "caravan_temperature",
)
_WATCH_RE = re.compile("|".join(re.escape(s) for s in WATCHED_ENTITY_SUBSTRINGS))

def _is_watched(eid: str) -> bool:
    domain = eid.split(".")[0] if "." in eid else ""
    if domain in WATCHED_FULL_DOMAINS:
        return True
    return bool(_WATCH_RE.search(eid))
```

In `compute_state_diff`'s snapshot loop, after computing `eid`, skip anything not watched so it never enters the snapshot or diff:

```python
        if not _is_watched(eid):
            continue
```

(Leave the existing numeric 2.0/5% threshold logic — it still applies to watched numeric entities like `caravan_temperature`.)

- [ ] **Step 6: Run to verify pass**

Run: `python -m pytest tests/test_scheduler.py -v`
Expected: PASS. Update/replace any existing diff test that assumed the old watch-everything behaviour.

- [ ] **Step 7: Commit**

```bash
git add scheduler.py tests/test_scheduler.py
git commit -m "feat: proactive diff is opt-in allow-list, not watch-everything"
```

> **Note for execution:** this is the single highest-leverage change for token cost — it takes the ~96 daily polls that currently each wake Sonnet down to only those cycles where a genuinely-watched entity changed. Tune `WATCHED_ENTITY_SUBSTRINGS` from the Step 2 evidence before deploying; under-including is safer than over-including (Jarvis still answers anything you ask interactively).

---

### Task 5: Real dedup (full recent-message memory)

Replace the truncated `_recent_alerts` (deque maxlen=5, `stripped[:200]` at line 556) with full untruncated recent messages + timestamps, and feed them into the proactive context (bot.py builds this at line 176–179).

**Files:**
- Modify: `agents/conversation.py` (line 503 init; line 556 append)
- Modify: `bot.py` (`proactive_poll` lines 175–186)
- Test: `tests/test_conversation.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_conversation.py (add)
def test_recent_alerts_not_truncated():
    from jarvis.agents.conversation import ConversationAgent
    a = ConversationAgent(ha_client=None, send_fn=None)
    long_msg = "x" * 500
    a._record_sent(long_msg)
    assert a._recent_alerts[-1].endswith("x" * 50)   # full text retained, not cut at 200
    assert len(a._recent_alerts[-1]) >= 500
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_conversation.py -k recent_alerts_not_truncated -v`
Expected: FAIL (`_record_sent` undefined).

- [ ] **Step 3: Implement**

In `ConversationAgent.__init__` (line 503) keep the deque but widen it:

```python
        self._recent_alerts: deque[str] = deque(maxlen=8)
```

Add a helper method on the class:

```python
    def _record_sent(self, text: str) -> None:
        self._recent_alerts.append(text.strip())
```

Replace the truncating append in `run_proactive` (line 556):

```python
                self._record_sent(stripped)
```

In `bot.py:proactive_poll` (lines 176–179) feed the full recent messages with an explicit instruction:

```python
    async def proactive_poll(diff_text: str) -> None:
        recent = list(agent._recent_alerts)
        context_parts = [f"Home state changes since last poll:\n{diff_text}"]
        if recent:
            context_parts.append(
                "Recent messages already sent (do NOT repeat their content):\n"
                + "\n".join(f"- {a}" for a in recent)
            )
        context = "\n\n".join(context_parts)
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_conversation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/conversation.py bot.py tests/test_conversation.py
git commit -m "fix: full untruncated recent-message memory for dedup"
```

---

### Task 6: Starve the proactive tool loop (token guard)

Proactive runs can call `get_states` (whole-house dump) and loop up to `MAX_TOOL_ROUNDS=5`, each round re-sending accumulated context. Cap proactive rounds lower and prevent the broad dump in proactive mode.

**Files:**
- Modify: `agents/conversation.py` (`_run_with_tools` line 563; add `MAX_PROACTIVE_TOOL_ROUNDS`)
- Test: `tests/test_conversation.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_conversation.py (add)
def test_proactive_round_cap_is_lower():
    from jarvis.agents import conversation
    assert conversation.MAX_PROACTIVE_TOOL_ROUNDS < conversation.MAX_TOOL_ROUNDS
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_conversation.py -k proactive_round_cap -v`
Expected: FAIL (constant undefined).

- [ ] **Step 3: Implement**

Near line 29 add:

```python
MAX_PROACTIVE_TOOL_ROUNDS = 2
```

In `_run_with_tools` (line 563), use the lower cap in proactive mode. Replace the round-limit line (570 `while rounds < MAX_TOOL_ROUNDS:`):

```python
        round_cap = MAX_PROACTIVE_TOOL_ROUNDS if mode == "proactive" else MAX_TOOL_ROUNDS
        while rounds < round_cap:
```

In the proactive operational guidance (`_MODE_LAYERS["proactive"]` from Task 1) append a line discouraging the broad dump:

```
        "Work from the change summary you were given. Do not call get_states (it dumps the whole house); "
        "if you must check one entity, use get_state with a specific id."
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_conversation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/conversation.py tests/test_conversation.py
git commit -m "perf: cap proactive tool rounds and discourage whole-house get_states"
```

---

### Task 7: Model promotion + cacheable prefix

Promote `BRIEFING_MODEL` and `CONVERSATION_MODEL` defaults to Sonnet, and inject current time via the user message so the static system prefix is cacheable.

**Files:**
- Modify: `config.py` (lines 17–22)
- Modify: `agents/conversation.py` (`reply` / `run_proactive` — prepend a small time note to the user message)
- Test: `tests/test_config.py`, `tests/test_conversation.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py (add)
def test_voice_models_default_to_sonnet(monkeypatch):
    for v in ("BRIEFING_MODEL", "CONVERSATION_MODEL"):
        monkeypatch.delenv(v, raising=False)
    import importlib, jarvis.config as c
    importlib.reload(c)
    assert "sonnet" in c.config.BRIEFING_MODEL.lower()
    assert "sonnet" in c.config.CONVERSATION_MODEL.lower()

# tests/test_conversation.py (add)
def test_current_time_not_in_static_prompt():
    from jarvis.agents import conversation
    p = conversation._load_system_prompt(mode="conversation")
    assert "Current local date and time" not in p
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_config.py -k sonnet tests/test_conversation.py -k current_time_not_in_static -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`config.py` lines 17–22:

```python
    BRIEFING_MODEL: str = os.environ.get(
        "BRIEFING_MODEL", "openrouter/anthropic/claude-sonnet-4-6"
    )
    CONVERSATION_MODEL: str = os.environ.get(
        "CONVERSATION_MODEL", "openrouter/anthropic/claude-sonnet-4-6"
    )
```

Ensure the static prompt has no time line (already removed in Task 1 Step 3). Inject time into the user turn instead — in `reply` (line ~508) and in `run_proactive` where the `[PROACTIVE]` message is built (line ~543), prefix the user content with a one-line time stamp:

```python
        history.append({"role": "user", "content": f"(now: {_now_str()})\n{user_text}"})
```

```python
            messages.append({"role": "user", "content": f"(now: {_now_str()}) [PROACTIVE] {context}"})
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_config.py tests/test_conversation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add config.py agents/conversation.py tests/test_config.py tests/test_conversation.py
git commit -m "feat: Sonnet for voice paths; move clock out of static prefix for caching"
```

> **Planning-time decision (D6):** if OpenRouter does not honour Anthropic prompt caching, switch the Sonnet paths to a direct `anthropic/claude-sonnet-4-6` model string (uses `ANTHROPIC_API_KEY`) and add `cache_control` breakpoints in `router.complete`. Confirm cache support empirically (Task 8 token logs) before deciding; leave a note in the commit if deferred.

---

### Task 8: Token logging (so savings are measurable)

`router.complete` (router.py) discards usage. Log prompt/completion/total tokens per call so the noise/caching wins are observable.

**Files:**
- Modify: `router.py` (`complete`)
- Test: `tests/test_router.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_router.py (add)
async def test_complete_logs_token_usage(mock_env, caplog):
    import logging, jarvis.router as router
    caplog.set_level(logging.INFO)
    # mock litellm.acompletion to return a response with usage
    class _Usage: prompt_tokens=100; completion_tokens=20; total_tokens=120
    class _Msg: content="hi"
    class _Choice: message=_Msg()
    class _Resp: choices=[_Choice()]; usage=_Usage()
    async def fake(*a, **k): return _Resp()
    router.litellm.acompletion = fake
    await router.complete("triage", [{"role": "user", "content": "x"}])
    assert any("tokens" in r.message.lower() for r in caplog.records)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_router.py -k logs_token_usage -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `router.py`, add a module logger and log usage after the call:

```python
import logging
logger = logging.getLogger(__name__)
```

Replace the return in `complete`:

```python
    usage = getattr(response, "usage", None)
    if usage is not None:
        logger.info(
            "%s tokens: prompt=%s completion=%s total=%s",
            agent,
            getattr(usage, "prompt_tokens", "?"),
            getattr(usage, "completion_tokens", "?"),
            getattr(usage, "total_tokens", "?"),
        )
    return response.choices[0].message.content
```

- [ ] **Step 4: Run to verify pass**

Run: `python -m pytest tests/test_router.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add router.py tests/test_router.py
git commit -m "obs: log per-call token usage in router"
```

---

### Task 9: Full verification + deploy

**Files:** none (verification + deployment of gitignored files on the box)

- [ ] **Step 1: Full suite green**

Run: `python -m pytest tests/ -v`
Expected: ALL PASS. Fix any regressions before proceeding.

- [ ] **Step 2: Apply gitignored changes on the deployed box** (`/homeassistant/jarvis`)

- Update `soul.md` on the box with the Task 3 retargeting (the box has the real `soul.md`, not the `.example`).
- Update `.env`: set `BRIEFING_MODEL` and `CONVERSATION_MODEL` to `openrouter/anthropic/claude-sonnet-4-6` (or direct `anthropic/...` per D6).
- Deploy code: copy the changed `.py` files to `/homeassistant/jarvis/` (or `git pull` if the box tracks this branch).

- [ ] **Step 3: Restart and smoke-test**

```bash
cd /homeassistant/jarvis && ./start.sh
tail -n 50 -f jarvis.log
```

Confirm in the log: (a) most `insight_poll` cycles now log "no meaningful change" and do NOT call the proactive model; (b) `... tokens: prompt=...` lines appear; (c) the next morning briefing reads in Jarvis's voice and omits repeated standing facts.

- [ ] **Step 4: Finish the branch**

Use superpowers:finishing-a-development-branch to decide merge/PR. Record measured before/after token volume from the new logs in the PR description.

---

## Self-Review Notes
- **Spec coverage:** Component 1 → Task 1; Component 2 (briefing voice) → Task 2; soul/memory reconciliation → Task 3; Component 3 (diff noise) → Task 4; Component 4 (dedup) → Task 5; Component 6 token/tool-loop → Task 6 + Task 8; Component 5 models + caching → Task 7. Deploy of gitignored persona/.env → Task 9. All six components covered.
- **Out of scope (confirmed):** routines re-platform; the rec-engine branch; triage→Haiku bump (D5, deliberately deferred).
- **Type consistency:** `_load_system_prompt(mode)`, `_run_with_tools(..., mode=...)`, `_record_sent`, `MAX_PROACTIVE_TOOL_ROUNDS`, `_is_excluded` are defined where first used and referenced consistently.
- **Known risk:** exact line numbers are from `main` as of `f361e51`; the executor should match on surrounding text, not raw line numbers, since earlier tasks shift lines.
