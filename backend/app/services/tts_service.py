import json
import logging
import os
import random

import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer

from app import config
from app.models import get_video_dir

logger = logging.getLogger(__name__)

# Configure DashScope API key (reuse the existing ASR key)
dashscope.api_key = config.DASHSCOPE_API_KEY

# Speaker -> voice mapping cache (per video)
_speaker_voice_map: dict[str, dict[str, str]] = {}


def assign_voice_for_speaker(video_id: str, speaker_id: str) -> str:
    """
    Assign a unique voice to a speaker within a video.
    Uses random selection without replacement so different speakers get different voices.
    """
    if video_id not in _speaker_voice_map:
        _speaker_voice_map[video_id] = {}

    mapping = _speaker_voice_map[video_id]

    if speaker_id in mapping:
        return mapping[speaker_id]

    # Find voices not yet assigned in this video
    used_voices = set(mapping.values())
    available = [v for v in config.TTS_VOICES if v not in used_voices]

    if not available:
        # All voices used, allow reuse
        available = list(config.TTS_VOICES)

    voice = random.choice(available)
    mapping[speaker_id] = voice
    logger.info(f"[{video_id}] Assigned voice '{voice}' to speaker '{speaker_id}'")
    return voice


def get_speaker_voice_map(video_id: str) -> dict[str, str]:
    """Get the current speaker->voice mapping for a video."""
    return _speaker_voice_map.get(video_id, {})


async def synthesize_speech(
    video_id: str,
    segment_id: str,
    text: str,
    voice: str | None = None,
) -> str:
    """
    Synthesize speech using Alibaba DashScope CosyVoice3-flash.
    Uses non-streaming (blocking) call mode.

    Args:
        video_id: MD5 hash of the video
        segment_id: Unique ID for the segment
        text: Text to synthesize
        voice: Voice name (optional, uses config default)

    Returns:
        Path to the saved audio file
    """
    voice = voice or config.TTS_DEFAULT_VOICE

    logger.info(f"Submitting TTS task to DashScope ({config.TTS_MODEL}), Voice: {voice} for {segment_id}")

    import asyncio
    loop = asyncio.get_event_loop()

    def _do_tts():
        synthesizer = SpeechSynthesizer(
            model=config.TTS_MODEL,
            voice=voice,
        )
        audio = synthesizer.call(text)
        return audio

    # Run blocking DashScope call in thread pool
    audio = await loop.run_in_executor(None, _do_tts)

    if not audio:
        raise RuntimeError(f"TTS returned empty audio for segment {segment_id}")

    # Save to disk in tts/ subfolder
    video_dir = get_video_dir(video_id)
    tts_dir = os.path.join(video_dir, "tts")
    os.makedirs(tts_dir, exist_ok=True)

    audio_path = os.path.join(tts_dir, f"{segment_id}.mp3")
    try:
        with open(audio_path, "wb") as f:
            f.write(audio)
        logger.info(f"Saved synthesized audio to {audio_path}")

        # Consistent persistence: Update tts_results.json
        results_path = os.path.join(video_dir, "tts_results.json")
        tts_results = {}
        if os.path.exists(results_path):
            try:
                with open(results_path, "r", encoding="utf-8") as f:
                    tts_results = json.load(f)
            except Exception:
                pass

        tts_results[segment_id] = f"tts/{segment_id}.mp3"

        with open(results_path, "w", encoding="utf-8") as f:
            json.dump(tts_results, f, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.warning(f"Failed to save synthesized audio or update registry: {e}")

    return audio_path
