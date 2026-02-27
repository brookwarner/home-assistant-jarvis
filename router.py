from __future__ import annotations
import os
from pathlib import Path
import litellm
from typing import Any

# Load .env explicitly so keys are available regardless of how the process was launched
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

# Suppress litellm verbose output
litellm.set_verbose = False


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
        fallbacks.append("claude-haiku-4-5-20251001")
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

    # Pass api_key explicitly for OpenRouter models so auth isn't env-var dependent
    extra: dict = {}
    if model.startswith("openrouter/") and config.OPENROUTER_API_KEY:
        extra["api_key"] = config.OPENROUTER_API_KEY

    response = await litellm.acompletion(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        fallbacks=fallbacks,
        **extra,
        **kwargs,
    )
    return response.choices[0].message.content
