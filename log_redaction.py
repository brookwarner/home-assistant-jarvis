"""Keep secrets out of the add-on log.

Two things leak tokens into `ha addons logs local_jarvis`:

  * httpx logs the full request URL at INFO, and python-telegram-bot polls
    `https://api.telegram.org/bot<TOKEN>/getUpdates` every few seconds — so the
    live bot token was written to the log ring buffer continuously.
  * Any traceback out of httpx/telegram carries the same URL.

Silencing httpx alone would fix the first case only, so the redaction is done at
the *handler* level: every record reaching a root handler is rewritten,
whichever logger emitted it.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

REDACTED = "<redacted>"

# Telegram bot tokens look like `123456789:AAF-abc...` (numeric id, colon, ~35
# chars of base64url). Matching the shape rather than the configured value means
# a token from an old config, a second bot, or an error string still gets caught.
# Not `\b` before the digits: in the URL that leaks these (`.../bot123456789:AA…`)
# the digits follow a word char, so there is no boundary to match.
_TELEGRAM_TOKEN = re.compile(r"(?<!\d)\d{6,}:[A-Za-z0-9_-]{30,}")

# Anthropic/OpenRouter/Groq keys are matched by shape too, as a backstop for the
# literal values passed in from config.
_BEARER_LIKE = re.compile(r"(?<![A-Za-z0-9])(?:sk-ant-|sk-or-|gsk_)[A-Za-z0-9_-]{20,}")


class SecretRedactingFilter(logging.Filter):
    """Rewrite known secrets out of a record's message and traceback."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # Longest first, so a token that contains another as a prefix redacts whole.
        self._literals = sorted(
            {s for s in secrets if s and len(s) >= 12},
            key=len,
            reverse=True,
        )

    def redact(self, text: str) -> str:
        for secret in self._literals:
            text = text.replace(secret, REDACTED)
        text = _TELEGRAM_TOKEN.sub(REDACTED, text)
        text = _BEARER_LIKE.sub(REDACTED, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, let it through
            return True

        redacted = self.redact(message)
        if redacted != message:
            # Args are already interpolated into `redacted`; clearing them stops
            # the formatter interpolating a second time (and re-leaking).
            record.msg = redacted
            record.args = ()

        # Formatter.format() reuses a pre-set exc_text, so redact the traceback
        # here rather than letting the handler render the raw one.
        if record.exc_info and not record.exc_text:
            record.exc_text = self.redact(
                logging.Formatter().formatException(record.exc_info)
            )
        elif record.exc_text:
            record.exc_text = self.redact(record.exc_text)

        return True


def install_log_redaction(secrets: Iterable[str] = ()) -> SecretRedactingFilter:
    """Attach the redacting filter to every current root handler.

    Call after logging is configured. Handlers added later are not covered, so
    add them before calling this (the add-on only uses basicConfig's handler).
    """
    log_filter = SecretRedactingFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_filter)
    return log_filter


def quiet_http_client_logs() -> None:
    """Drop httpx/httpcore to WARNING unless the operator asked for DEBUG.

    The per-poll "HTTP Request: POST .../getUpdates 200 OK" line is pure noise at
    a ~5s cadence. At DEBUG the operator wants HTTP detail, so leave it alone —
    the redaction filter keeps it safe either way.
    """
    if logging.getLogger().getEffectiveLevel() > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
