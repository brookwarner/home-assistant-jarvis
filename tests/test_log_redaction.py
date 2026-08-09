import io
import logging

import pytest

from jarvis.log_redaction import (
    REDACTED,
    SecretRedactingFilter,
    install_log_redaction,
    quiet_http_client_logs,
)

# Shaped like a real Telegram token, but not one.
FAKE_TOKEN = "123456789:AAFtesttesttesttesttesttesttesttest1"
POLL_LINE = (
    'HTTP Request: POST https://api.telegram.org/bot%s/getUpdates "HTTP/1.1 200 OK"'
    % FAKE_TOKEN
)


@pytest.fixture
def captured():
    """A root handler writing to a buffer, with the redaction filter installed."""
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    install_log_redaction([FAKE_TOKEN])
    yield stream
    root.handlers, root.level = old_handlers, old_level


def test_poll_line_no_longer_contains_the_token(captured):
    logging.getLogger("httpx").info(POLL_LINE)
    out = captured.getvalue()
    assert FAKE_TOKEN not in out
    assert REDACTED in out
    # The rest of the line survives — this is redaction, not suppression.
    assert "getUpdates" in out and "200 OK" in out


def test_redacts_a_token_that_is_not_the_configured_one():
    other = "987654321:BBQotherotherotherotherotherother12"
    assert other not in SecretRedactingFilter([FAKE_TOKEN]).redact(f"url /bot{other}/x")


def test_redacts_secrets_passed_via_lazy_args(captured):
    logging.getLogger("telegram").warning("failed calling %s", POLL_LINE)
    out = captured.getvalue()
    assert FAKE_TOKEN not in out
    assert "failed calling" in out


def test_redacts_tokens_inside_tracebacks(captured):
    try:
        raise RuntimeError(f"connect failed for https://api.telegram.org/bot{FAKE_TOKEN}/x")
    except RuntimeError:
        logging.getLogger("telegram.ext").exception("poll failed")
    out = captured.getvalue()
    assert FAKE_TOKEN not in out
    assert "RuntimeError" in out


def test_redacts_api_keys_by_shape():
    f = SecretRedactingFilter()
    redacted = f.redact("auth sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345 done")
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz012345" not in redacted
    assert redacted.endswith(" done")


def test_ordinary_lines_are_untouched():
    line = "Briefing sent to 123456 at 08:00"
    assert SecretRedactingFilter([FAKE_TOKEN]).redact(line) == line


def test_http_loggers_quieted_at_info_but_not_at_debug():
    root = logging.getLogger()
    old_level = root.level
    httpx_log = logging.getLogger("httpx")
    old_httpx = httpx_log.level
    try:
        root.setLevel(logging.INFO)
        httpx_log.setLevel(logging.NOTSET)
        quiet_http_client_logs()
        assert httpx_log.level == logging.WARNING

        root.setLevel(logging.DEBUG)
        httpx_log.setLevel(logging.NOTSET)
        quiet_http_client_logs()
        assert httpx_log.level == logging.NOTSET
    finally:
        root.setLevel(old_level)
        httpx_log.setLevel(old_httpx)
