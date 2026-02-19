import base64
import logging
import os

from openai import AsyncOpenAI
from app import config
from app.models import get_video_dir

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=config.TTS_API_KEY,
    base_url=config.TTS_BASE_URL,
    timeout=config.TTS_TIMEOUT,
)


async def synthesize_speech(video_id: str, segment_id: str, text: str, voice: str | None = None) -> tuple[str, str]:
    """
    Synthesize speech from text using OpenAI TTS API and save to disk.
    
    Args:
        video_id: MD5 hash of the video
        segment_id: Unique ID for the segment
        text: Text to synthesize
        voice: Voice name (optional, uses config default)
    
    Returns:
        Tuple of (base64_audio_data, content_type)
    """
    voice = voice or config.TTS_VOICE
    
    logger.info(f"Submitting TTS task to SiliconFlow ({config.TTS_MODEL}), Voice: {voice} for {segment_id}")

    response = await _client.audio.speech.create(
        model=config.TTS_MODEL,
        voice=voice,
        input=text,
        response_format="mp3",
    )

    # Save to disk in tts/ subfolder
    video_dir = get_video_dir(video_id)
    tts_dir = os.path.join(video_dir, "tts")
    os.makedirs(tts_dir, exist_ok=True)
    
    audio_path = os.path.join(tts_dir, f"{segment_id}.mp3")
    try:
        with open(audio_path, "wb") as f:
            f.write(response.content)
        logger.info(f"Saved synthesized audio to {audio_path}")
        
        # Consistent persistence: Update tts_results.json
        # This keeps a record of which segments have valid audio
        results_path = os.path.join(video_dir, "tts_results.json")
        tts_results = {}
        if os.path.exists(results_path):
            try:
                with open(results_path, "r", encoding="utf-8") as f:
                    tts_results = json.load(f)
            except Exception:
                pass
        
        # Save relative path or just a flag. Using relative path for robustness.
        tts_results[segment_id] = f"tts/{segment_id}.mp3"
        
        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(tts_results, f, ensure_ascii=False, indent=2)
            
    except Exception as e:
        logger.warning(f"Failed to save synthesized audio or update registry: {e}")

    # Return base64 for frontend immediate use
    audio_base64 = base64.b64encode(response.content).decode("utf-8")
    return audio_base64, "audio/mp3"
