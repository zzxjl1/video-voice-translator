import asyncio
import json
import logging
import os
import random
import subprocess
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


def _clamp_rate(rate: float) -> float:
    """Clamp to the range the API accepts (verified 0.5–2.0)."""
    return max(config.TTS_RATE_MIN, min(config.TTS_RATE_MAX, rate))


def _probe_duration(path: str) -> float | None:
    """
    Real duration of a synthesized file, in seconds, or None if ffprobe failed.

    This is free — the audio is already on disk — and it is what turns the
    duration prediction from a guess into something verifiable.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
        logger.warning(f"ffprobe could not read {path}: {result.stderr[-200:]}")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"ffprobe failed on {path}: {e}")
    return None


def _synthesize_blocking(
    text: str, voice: str, speech_rate: float | None = None
) -> bytes:
    """
    One blocking synthesis call.

    `speech_rate` is only sent when the caller provided one: the API's default
    is 1.0 and an unset value keeps the previous behaviour exactly (important
    for the manual re-synthesis and preview paths).
    """
    kwargs: dict = {"model": config.TTS_MODEL, "voice": voice}
    if speech_rate is not None:
        kwargs["speech_rate"] = speech_rate
    return SpeechSynthesizer(**kwargs).call(text)


async def _synthesize_with_retries(
    loop,
    text: str,
    voice: str,
    speech_rate: float | None,
    segment_id: str,
) -> bytes:
    """Synthesize with the configured retry/backoff. Raises on total failure."""
    last_error: Exception | None = None
    for attempt in range(1, config.TTS_MAX_RETRIES + 2):
        try:
            audio = await loop.run_in_executor(
                None, _synthesize_blocking, text, voice, speech_rate
            )
            if audio:
                return audio
            last_error = RuntimeError("TTS returned empty audio")
        except Exception as e:
            last_error = e

        if attempt <= config.TTS_MAX_RETRIES:
            logger.warning(
                f"TTS attempt {attempt} failed for {segment_id}: {last_error}. Retrying..."
            )
            await asyncio.sleep(1.0 * attempt)

    raise RuntimeError(f"TTS failed for segment {segment_id}: {last_error}")


def _write(path: str, payload: bytes) -> None:
    with open(path, "wb") as f:
        f.write(payload)


async def _write_fitted_audio(
    loop,
    audio_path: str,
    first_take: bytes,
    text: str,
    voice: str,
    speech_rate: float | None,
    target_duration: float | None,
    segment_id: str,
    video_id: str,
) -> tuple[float | None, float | None]:
    """
    Store `first_take`, then measure it and correct it if it misses the slot.

    Measuring is free — the file is already on disk — so the only cost of
    fitting is the extra synthesis, and only for lines that land more than
    `config.TTS_FIT_TOLERANCE` away from their target.

    The correction is exact rather than approximate: the speech_rate/duration
    relation was measured to be strictly linear, so `rate * (actual / target)`
    lands on target in a single step.

    Returns (final_duration, applied_rate) for logging.
    """
    _write(audio_path, first_take)

    if not target_duration or target_duration <= 0:
        return _probe_duration(audio_path), speech_rate

    actual = _probe_duration(audio_path)
    if actual is None:
        return None, speech_rate

    residual = (actual - target_duration) / target_duration
    if abs(residual) <= config.TTS_FIT_TOLERANCE:
        return actual, speech_rate

    applied = speech_rate if speech_rate is not None else 1.0
    corrected = _clamp_rate(applied * (actual / target_duration))
    if abs(corrected - applied) < 0.01:
        # Already at the API's speed limit; nothing more to be done here.
        logger.info(
            "[%s] %s missed its slot by %+.0f%% but is already at the rate limit "
            "(%.2fx); leaving it for the player to stretch.",
            video_id, segment_id, residual * 100, applied,
        )
        return actual, applied

    logger.info(
        "[%s] %s missed its slot by %+.0f%% (%.2fs vs %.2fs) -> re-synthesizing at %.2fx",
        video_id, segment_id, residual * 100, actual, target_duration, corrected,
    )
    try:
        retry = await _synthesize_with_retries(loop, text, voice, corrected, segment_id)
    except Exception as e:
        # Keep the first take: it is usable, just slightly long or short.
        logger.warning(
            "[%s] Re-synthesis of %s failed (%s); keeping the first take.",
            video_id, segment_id, e,
        )
        return actual, applied

    _write(audio_path, retry)
    final = _probe_duration(audio_path)
    logger.info(
        "[%s] %s fitted: %.2fs -> %s (target %.2fs)",
        video_id, segment_id, actual,
        f"{final:.2f}s" if final else "unknown", target_duration,
    )
    return (final or actual), corrected


async def synthesize_speech(
    video_id: str,
    segment_id: str,
    text: str,
    voice: str | None = None,
    write_registry: bool = True,
    speech_rate: float | None = None,
    target_duration: float | None = None,
) -> str:
    """
    Synthesize speech using Alibaba DashScope Qwen-Audio-TTS.
    Uses non-streaming (blocking) call mode.

    Args:
        video_id: MD5 hash of the video
        segment_id: Unique ID for the segment
        text: Text to synthesize
        voice: Voice name (optional, uses config default)
        write_registry: Whether to update tts_results.json (set False when the
            caller batches the registry write itself).
        speech_rate: Speed to synthesize at (0.5–2.0). None keeps the default.
            `llm_service.plan_speech_rate()` predicts this from the text.
        target_duration: The time slot this line has to fill. When given, the
            result is measured with ffprobe and — only if it misses by more than
            `config.TTS_FIT_TOLERANCE` — re-synthesized once at a corrected rate.
            That costs one extra call for the segments that need it, and nothing
            for the rest.

    Returns:
        Path to the saved audio file
    """
    voice = voice or config.TTS_DEFAULT_VOICE

    logger.info(
        f"Submitting TTS task to DashScope ({config.TTS_MODEL}), Voice: {voice}, "
        f"rate: {speech_rate if speech_rate is not None else 'default'} for {segment_id}"
    )

    loop = asyncio.get_event_loop()

    # The SDK call is blocking -> run it in the thread pool so the event loop
    # (and other concurrent segment jobs) keep making progress.
    audio = await _synthesize_with_retries(loop, text, voice, speech_rate, segment_id)

    # Save to disk in tts/ subfolder
    video_dir = get_video_dir(video_id)
    tts_dir = os.path.join(video_dir, "tts")
    os.makedirs(tts_dir, exist_ok=True)
    audio_path = os.path.join(tts_dir, f"{segment_id}.mp3")

    try:
        actual, applied = await _write_fitted_audio(
            loop,
            audio_path,
            audio,
            text,
            voice,
            speech_rate,
            target_duration,
            segment_id,
            video_id,
        )
        detail = f", {actual:.2f}s" if actual else ""
        detail += f" @ rate {applied:.2f}" if applied else ""
        logger.info(f"Saved synthesized audio to {audio_path}{detail}")

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
