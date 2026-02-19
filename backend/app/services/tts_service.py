"""
TTS service via OpenAI SDK.
Uses SiliconFlow's TTS endpoint to synthesize speech from text.
"""
import base64
import logging

from openai import AsyncOpenAI

from app import config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=config.TTS_API_KEY,
    base_url=config.TTS_BASE_URL,
    timeout=config.TTS_TIMEOUT,
)


async def synthesize_speech(text: str, voice: str | None = None) -> tuple[str, str]:
    """
    Synthesize speech from text using OpenAI TTS API.
    
    Args:
        text: Text to synthesize
        voice: Voice name (optional, uses config default)
    
    Returns:
        Tuple of (base64_audio_data, content_type)
    """
    voice = voice or config.TTS_VOICE
    
    logger.info(f"Submitting TTS task to SiliconFlow ({config.TTS_MODEL}), Voice: {voice}")

    response = await _client.audio.speech.create(
        model=config.TTS_MODEL,
        voice=voice,
        input=text,
        response_format="mp3",
    )

    # response.content is bytes
    audio_base64 = base64.b64encode(response.content).decode("utf-8")
    return audio_base64, "audio/mp3"
