"""Voice message handling: download OGG -> Whisper -> text."""

import logging
import tempfile
from pathlib import Path

import httpx
import openai

from fitnessbot.config import Config

logger = logging.getLogger(__name__)


async def download_voice_file(file_url: str) -> bytes:
    """Download a voice file from Telegram's servers."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(file_url)
        resp.raise_for_status()
        return resp.content


def transcribe_audio(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe audio bytes using OpenAI Whisper API."""
    if not Config.OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set; voice transcription unavailable")
        return ""

    client = openai.OpenAI(api_key=Config.OPENAI_API_KEY)

    # Write to a temp file since the API expects a file-like object
    suffix = Path(filename).suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        tmp.seek(0)
        transcript = client.audio.transcriptions.create(
            model="whisper-1",
            file=tmp,
        )
    return transcript.text
