"""Voice message handling: download OGG -> Whisper -> text."""

import logging
import tempfile
from pathlib import Path

import httpx

from fitnessbot.config import Config

logger = logging.getLogger(__name__)

_local_model = None


def _get_local_model():
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel
        model_size = getattr(Config, "WHISPER_MODEL_SIZE", "base")
        logger.info("Loading local Whisper model: %s", model_size)
        _local_model = WhisperModel(model_size, device="cpu", compute_type="int8")
        logger.info("Local Whisper model loaded")
    return _local_model


async def download_voice_file(file_url: str) -> bytes:
    """Download a voice file from Telegram's servers."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(file_url)
        resp.raise_for_status()
        return resp.content


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe audio bytes using local faster-whisper, falling back to OpenAI Whisper API."""
    suffix = Path(filename).suffix or ".ogg"

    # Try local faster-whisper first
    try:
        model = _get_local_model()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
            tmp.write(audio_bytes)
            tmp.flush()
            segments, info = model.transcribe(tmp.name, beam_size=5)
            text = " ".join(seg.text.strip() for seg in segments).strip()
            if text:
                logger.info("Local transcription: %.1fs audio, language=%s", info.duration, info.language)
                return text
    except Exception as e:
        logger.warning("Local transcription failed: %s", e)

    # Fall back to OpenAI Whisper API if key is available
    if Config.OPENAI_API_KEY:
        try:
            import openai
            client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
                tmp.write(audio_bytes)
                tmp.flush()
                tmp.seek(0)
                transcript = client.audio.transcriptions.create(
                    model="whisper-1",
                    file=tmp,
                )
            return transcript.text
        except Exception as e:
            logger.warning("OpenAI Whisper fallback failed: %s", e)

    return ""
