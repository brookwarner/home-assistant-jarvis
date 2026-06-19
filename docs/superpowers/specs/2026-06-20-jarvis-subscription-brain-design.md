# Jarvis subscription brain — design

**Date:** 2026-06-20
**Status:** Approved for planning
**Author:** Brook + Claude

## Problem

Jarvis runs its LLM calls through the **direct Anthropic API**, costing **~$22 month-to-date (~$40–50/mo at current pace)** — almost entirely Claude Sonnet 4.6. Meanwhile a Claude **Max 5x** subscription ($100/mo) sits with idle quota. The goal is to shift Jarvis's LLM workload onto the subscription instead of paying per-token, while keeping the assistant fully functional and local.

The naive idea ("convert it to a cloud routine triggered via webhook") does not fit: subscription quota is only spendable programmatically by driving the **Claude Agent SDK / `claude` CLI**, and the expensive part of Jarvis (the interactive, tool-calling conversation agent) needs low-latency, live access to the *local* Home Assistant — which a cloud routine cannot provide well. So the plan is to run a subscription-backed brain **locally**, not in the cloud.

## Findings from cost analysis (the basis for this design)

Measured from the live add-on logs (`local_jarvis`) over Jun 15–20 (364 LLM calls) and the Anthropic Console:

1. **The proactive poll is the volume driver.** `scheduler.py:insight_poll` diffs watched entities every 1–5 min and, when changes exceed a noise threshold, fires a full conversation call with all 24 tools — **~74 calls/day, 24/7** (hourly histogram stays at 10–15 calls/hr even at 1–5 AM; ~53% land on a fixed scheduled cadence). The scheduler comment already notes this: *"9–19 noisy sensor changes per 15-min poll each cost a Sonnet call."*
2. **Every call carries a large static prefix.** 352/357 conversation calls were 6,000–9,000 input tokens — the ~6,800-token prefix (system prompt + 24 tool definitions) dominates *every* call. `conversation.py:556` says the prompt is *"static across calls so it can be prompt-cached"*, but **no `cache_control` is set anywhere**, so the prefix is re-billed at full price on 99% of calls.
3. **Live config overrides code defaults to Sonnet.** Code defaults are all `anthropic/claude-haiku-4-5`; the box's `.env` overrides conversation to Sonnet 4.6. The log model string showed `openrouter/anthropic/claude-4.6-sonnet` while the Console billed direct Anthropic Sonnet 4.6 — a discrepancy to reconcile during implementation.
4. **`PROACTIVE_MODEL` may not be wired.** `config.PROACTIVE_MODEL` defaults to Haiku, but `router._get_model()` only maps triage/briefing/conversation. If `run_proactive` falls through to `self._model` (the conversation/Sonnet model), the 24/7 poll is running on the expensive model despite the cheap knob existing. **Verify first** — fixing this alone could be a large, instant cut.

## Goals

- Shift Jarvis's LLM workload onto the Max 5x subscription via the Claude Agent SDK, ToS-cleanly.
- Keep the assistant fully local and functional (Telegram chat, proactive alerts, morning briefing, 24 HA tools).
- Stay within Max 5x usage limits **without starving interactive Claude Code use** on the same subscription.
- Keep the existing API path as a working, cheap fallback.

## Non-goals

- No cloud-hosted routine for the interactive agent (architectural mismatch).
- No scripting of subscription OAuth into raw API calls (ToS violation).
- Not rewriting the tool implementations — tool *bodies* (`_execute_tool`) are reused as-is.

## Constraints / decisions (confirmed with Brook)

- **Subscription tier:** Max 5x. The 24/7 proactive volume must be cut hard to fit, and routine work routed to Haiku-tier (cheaper against limits).
- **Auth:** Jarvis gets its **own** one-time `claude login` (OAuth device-code) inside the add-on container; token persists in `/config`, CLI auto-refreshes. (The `claude_terminal` add-on's login lives in a separate container and is not reliably shareable.)
- **Rollout:** incremental — cheap API-path wins first, then move agents onto the subscription one at a time.

## Architecture

### Backend switch

Introduce a backend abstraction so the rest of Jarvis is agnostic to *how* the LLM is reached:

- New module `brain.py` wraps the **Claude Agent SDK** (Python), which drives the locally-installed `claude` CLI authenticated with the Max subscription. It exposes the same surface the codebase already calls — a `complete()`-style entry and a tool-running entry equivalent to `_run_with_tools`.
- `router.py` becomes a thin switch on a new `LLM_BACKEND` config value:
  - `LLM_BACKEND=subscription` → `brain.py` (Agent SDK / subscription).
  - `LLM_BACKEND=api` → today's `litellm.acompletion` path (unchanged, kept as fallback).
- The switch can be **per-agent** (e.g. briefing on subscription, proactive on API) to support incremental rollout and A/B comparison.

### Tool porting

- The 24 entries in `conversation.py:TOOLS` (OpenAI/litellm function-call schema) are registered as **Agent-SDK tools**. Each tool's handler calls the existing `_execute_tool(name, inputs)` body — no behavioural change to the tools themselves.
- The Agent SDK runs the agentic loop and tool dispatch, so the hand-rolled `_run_with_tools` round loop (and `MAX_TOOL_ROUNDS` / `MAX_PROACTIVE_TOOL_ROUNDS`) is replaced by the SDK's loop (with an equivalent round/turn cap).
- The static system prompt + volatile user-message split (`_load_system_prompt` + `(now: …)` injection) is preserved; the SDK's own prompt caching benefits from the stable prefix.

### Model tiering (against subscription limits)

- Proactive + triage → Haiku-tier (lowest impact on Max 5x limits).
- Interactive chat → Sonnet.
- Heavy/long tasks → Opus delegate (rare).

### Auth & packaging

- Add Node.js + the `claude` CLI (and the Python Agent SDK) to `addon/Dockerfile` (currently `python:3.12-alpine`; may move to a base with Node, or add Node via apk).
- One-time setup: run `claude login` (device-code flow — open the printed URL once, approve). The credential persists in the add-on's `/config`-mapped storage so it survives restarts; the CLI refreshes it.
- Document the login step in the add-on README and the deploy workflow.

## Prerequisite work (Phase 1 — API path, ships first)

Independently valuable and protects subscription limits before any traffic moves:

1. **Verify/fix `PROACTIVE_MODEL` wiring** — ensure the proactive poll uses Haiku, not the conversation model.
2. **Tame the proactive poll:**
   - Overnight quiet window (e.g. 23:00–06:00: Haiku-only or skip).
   - Gate the expensive call behind the free llama triage so the heavy agent only fires when triage says "worth it."
   - Review noise threshold / standard cadence.
3. **Wire prompt caching on the API path** — add `cache_control` to the last tool/system block so the ~6,800-token prefix caches (~80–90% input cut, keeps the fallback cheap).

## Rollout phases

1. **Phase 1 (API path):** proactive-model fix + proactive taming + prompt caching. Immediate cost relief, near-zero risk, no new dependencies.
2. **Phase 2 (subscription auth + brain, briefing only):** package `claude` CLI + Agent SDK, do the one-time login, route the **briefing** agent through `brain.py`. Lowest volume — validates auth, tooling, and limits cheaply.
3. **Phase 3:** move interactive Telegram chat onto the subscription brain.
4. **Phase 4:** move the proactive poll onto the subscription brain, once Max 5x limits are observed to hold with headroom for interactive Claude Code use.

Each phase is shippable and reversible via `LLM_BACKEND`.

## Risks & mitigations

- **Usage-limit collision (Max 5x).** A 24/7 agent can exhaust the rolling/weekly caps and starve interactive use. *Mitigation:* Phase 1 taming + Haiku tiering + phased rollout with observation before Phase 4.
- **Auth fragility in a container.** OAuth token expiry / refresh inside the add-on. *Mitigation:* persist in `/config`, document re-login, add a health check / clear log line when auth fails.
- **Agent SDK behavioural differences** vs the current litellm loop (tool-call formatting, stop conditions, latency). *Mitigation:* keep the API path as fallback; A/B per-agent via the switch.
- **Add-on image growth** (Node + CLI). *Mitigation:* accept the size; aarch64 base with Node if alpine+Node is painful.
- **No persistent cost/usage ledger today** (totals reset on restart, OpenRouter returned `cost=n/a`). *Mitigation:* add lightweight persistent usage logging so subscription-limit consumption is observable.

## Open questions (resolve during planning/implementation)

- Exact Claude Agent SDK package + version that supports subscription (CLI-OAuth) auth headless on aarch64.
- Whether the Agent SDK can pin per-call models (Haiku/Sonnet/Opus) as cleanly as the current router.
- The live `.env` model strings on the box (reconcile the openrouter-vs-direct-Anthropic discrepancy) before changing anything.
- Whether `claude login` device-code flow completes cleanly from within the add-on container (network/redirect).
