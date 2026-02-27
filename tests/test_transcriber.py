import pytest
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.asyncio

async def test_transcribe_joins_segments():
    from jarvis.transcriber import transcribe

    mock_model = MagicMock()
    seg1 = MagicMock(); seg1.text = " Hello"
    seg2 = MagicMock(); seg2.text = " world."
    mock_model.transcribe.return_value = ([seg1, seg2], MagicMock())

    with patch("jarvis.transcriber._get_model", return_value=mock_model):
        result = await transcribe("/tmp/test.ogg")

    assert result == "Hello world."

async def test_transcribe_returns_error_string_on_failure():
    from jarvis.transcriber import transcribe

    with patch("jarvis.transcriber._get_model", side_effect=Exception("model load fail")):
        result = await transcribe("/tmp/test.ogg")

    assert "transcribe" in result.lower() or "error" in result.lower()
