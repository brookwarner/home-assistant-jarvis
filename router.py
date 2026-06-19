from __future__ import annotations
import os
import logging
from pathlib import Path
import litellm
from typing import Any

logger = logging.getLogger(__name__)

# Load .env explicitly so keys are available regardless of how the process was launched
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

# Suppress litellm verbose output
litellm.set_verbose = False


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


def _get_model(agent: str) -> str:
    from jarvis.config import config
    return {
        "triage": config.TRIAGE_MODEL,
        "briefing": config.BRIEFING_MODEL,
        "conversation": config.CONVERSATION_MODEL,
    }[agent]


def _get_fallbacks(agent: str) -> list[str] | None:
    from jarvis.config import config
    if agent in ("triage", "briefing"):
        fallbacks = []
        if config.GROQ_API_KEY:
            fallbacks.append("groq/llama-3.1-8b-instant")
        fallbacks.append("anthropic/claude-haiku-4-5")
        return fallbacks
    return None


async def complete(
    agent: str,
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 512,
    **kwargs: Any,
) -> str:
    from jarvis.config import config
    model = _get_model(agent)
    fallbacks = _get_fallbacks(agent)

    # Pass api_key explicitly so auth isn't env-var dependent
    extra: dict = {}
    if model.startswith("openrouter/") and config.OPENROUTER_API_KEY:
        extra["api_key"] = config.OPENROUTER_API_KEY
    elif model.startswith("anthropic/") and config.ANTHROPIC_API_KEY:
        extra["api_key"] = config.ANTHROPIC_API_KEY

    response = await litellm.acompletion(
        model=model,
        messages=build_cached_messages(messages, model),
        temperature=temperature,
        max_tokens=max_tokens,
        fallbacks=fallbacks,
        **extra,
        **kwargs,
    )
    from jarvis.usage import log_completion
    log_completion(response, agent)
    return response.choices[0].message.content
