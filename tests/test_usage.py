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
