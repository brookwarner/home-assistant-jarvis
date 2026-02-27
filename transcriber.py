from __future__ import annotations
import asyncio
import logging

logger = logging.getLogger(__name__)

_model_instance = None


def _get_model():
    global _model_instance
    if _model_instance is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise ImportError(
                "faster-whisper is not installed. "
                "ctranslate2 is required but unavailable on this platform."
            )
        from jarvis.config import config
        logger.info(f"Loading Whisper model: {config.WHISPER_MODEL}")
        _model_instance = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model_instance


async def transcribe(file_path: str) -> str:
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _transcribe_sync, file_path)
        return result
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        return f"[Could not transcribe audio: {e}]"


def _transcribe_sync(file_path: str) -> str:
    model = _get_model()
    segments, _ = model.transcribe(file_path, beam_size=1)
    text = "".join(s.text for s in segments).strip()
    return text
