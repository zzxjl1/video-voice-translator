import asyncio
import json
import logging
import os
import random
import threading

import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer

from app import config
from app.models import get_video_dir

logger = logging.getLogger(__name__)

# Configure DashScope API key (reuse the existing ASR key)
dashscope.api_key = config.DASHSCOPE_API_KEY

# Speaker -> voice mapping cache (per video)
_speaker_voice_map: dict[str, dict[str, str]] = {}

# `tts_results.json` is read-modify-written for every segment. The pipeline
# now synthesizes segments concurrently, so guard those writes with a lock.
_registry_lock = threading.Lock()


def assign_voice_for_speaker(
    video_id: str,
    speaker_id: str,
    language: str | None = None,
) -> str:
    """
    Assign a unique voice to a speaker within a video.

    Voices are drawn from the pool that can actually synthesize `language`.
    System voice IDs are model- *and* language-specific: most of the
    `qwen-audio-3.1-tts-flash` "精品中文" voices only speak Mandarin, and
    handing one of those to an English dub is rejected by the service rather
    than silently ignored. Selection is random without replacement so
    different speakers get different voices.
    """
    if video_id not in _speaker_voice_map:
        _speaker_voice_map[video_id] = {}

    mapping = _speaker_voice_map[video_id]

    if speaker_id in mapping:
        return mapping[speaker_id]

    pool = config.voices_for_language(language)
    used_voices = set(mapping.values())
    available = [v for v in pool if v not in used_voices]

    if not available:
        # More speakers than voices — reuse, the caller usually has voice
        # cloning enabled for multi-speaker videos anyway.
        available = list(pool)

    voice = random.choice(available)
    mapping[speaker_id] = voice
    logger.info(
        f"[{video_id}] Assigned voice '{voice}' to speaker '{speaker_id}' "
        f"(language={language or 'any'}, pool size={len(pool)})"
    )
    return voice


def get_speaker_voice_map(video_id: str) -> dict[str, str]:
    """Get the current speaker->voice mapping for a video."""
    return _speaker_voice_map.get(video_id, {})


def _register_segment_audio(video_dir: str, segment_id: str, rel_path: str) -> None:
    """Thread-safe registration of a synthesized segment in tts_results.json."""
    results_path = os.path.join(video_dir, "tts_results.json")
    with _registry_lock:
        tts_results = {}
        if os.path.exists(results_path):
            try:
                with open(results_path, "r", encoding="utf-8") as f:
                    tts_results = json.load(f)
            except Exception:
                pass

        tts_results[segment_id] = rel_path

        try:
            with open(results_path, "w", encoding="utf-8") as f:
                json.dump(tts_results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to update tts registry for {segment_id}: {e}")


def _synthesize_blocking(text: str, voice: str) -> bytes:
    synthesizer = SpeechSynthesizer(
        model=config.TTS_MODEL,
        voice=voice,
    )
    return synthesizer.call(text)


async def synthesize_speech(
    video_id: str,
    segment_id: str,
    text: str,
    voice: str | None = None,
    write_registry: bool = True,
) -> str:
    """
    Synthesize speech using Alibaba DashScope CosyVoice3-flash.
    Uses non-streaming (blocking) call mode.

    Args:
        video_id: MD5 hash of the video
        segment_id: Unique ID for the segment
        text: Text to synthesize
        voice: Voice name (optional, uses config default)
        write_registry: Whether to update tts_results.json (set False when the
            caller batches the registry write itself).

    Returns:
        Path to the saved audio file
    """
    voice = voice or config.TTS_DEFAULT_VOICE

    logger.info(
        f"Submitting TTS task to DashScope ({config.TTS_MODEL}), Voice: {voice} for {segment_id}"
    )

    loop = asyncio.get_event_loop()

    # The SDK call is blocking -> run it in the thread pool so the event loop
    # (and other concurrent segment jobs) keep making progress.
    audio = None
    last_error: Exception | None = None
    for attempt in range(1, config.TTS_MAX_RETRIES + 2):
        try:
            audio = await loop.run_in_executor(
                None, _synthesize_blocking, text, voice
            )
            if audio:
                break
            last_error = RuntimeError("TTS returned empty audio")
        except Exception as e:
            last_error = e

        if attempt <= config.TTS_MAX_RETRIES:
            logger.warning(
                f"TTS attempt {attempt} failed for {segment_id}: {last_error}. Retrying..."
            )
            await asyncio.sleep(1.0 * attempt)

    if not audio:
        raise RuntimeError(f"TTS failed for segment {segment_id}: {last_error}")

    # Save to disk in tts/ subfolder
    video_dir = get_video_dir(video_id)
    tts_dir = os.path.join(video_dir, "tts")
    os.makedirs(tts_dir, exist_ok=True)

    audio_path = os.path.join(tts_dir, f"{segment_id}.mp3")
    try:
        with open(audio_path, "wb") as f:
            f.write(audio)
        logger.info(f"Saved synthesized audio to {audio_path}")

        if write_registry:
            _register_segment_audio(video_dir, segment_id, f"tts/{segment_id}.mp3")
    except Exception as e:
        logger.warning(f"Failed to save synthesized audio or update registry: {e}")

    return audio_path


def sync_registry(video_id: str, segment_ids: list[str]) -> None:
    """Write all segment paths to tts_results.json in one shot (concurrent-safe)."""
    video_dir = get_video_dir(video_id)
    results_path = os.path.join(video_dir, "tts_results.json")

    with _registry_lock:
        tts_results = {}
        if os.path.exists(results_path):
            try:
                with open(results_path, "r", encoding="utf-8") as f:
                    tts_results = json.load(f)
            except Exception:
                pass

        for segment_id in segment_ids:
            tts_results[segment_id] = f"tts/{segment_id}.mp3"

        try:
            with open(results_path, "w", encoding="utf-8") as f:
                json.dump(tts_results, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to sync tts registry: {e}")
