import asyncio
import json
import logging
import os
import random
import re
import subprocess
import threading

import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer

from app import config
from app.models import get_video_dir
from app.services import usage_service

logger = logging.getLogger(__name__)

# Configure DashScope API key (reuse the existing ASR key)
dashscope.api_key = config.DASHSCOPE_API_KEY

# Speaker -> voice, persisted per video.
#
# Its own file rather than a field on `VideoState`, and `tts_results.json` is
# separate for the same reason: the pipeline holds a `VideoState` of its own,
# loaded once at the start of a run, while this module loads another. Every
# `save_state(state)` the pipeline makes would write back ITS copy of the voice
# map — the one from before any assignment happened — and wipe out whatever was
# just chosen here. A file only this module touches cannot be clobbered that
# way.
_VOICE_MAP_FILE = "speaker_voices.json"

# Read once per video per process: the pipeline calls
# `assign_voice_for_speaker` for EVERY segment, and re-reading the file each
# time would be pure waste. Safe to cache because every writer lives in this
# module.
_voice_map_cache: dict[str, dict[str, str]] = {}
_voice_map_lock = threading.Lock()

# `tts_results.json` is read-modify-written for every segment. The pipeline
# now synthesizes segments concurrently, so guard those writes with a lock.
_registry_lock = threading.Lock()


def _voice_map_path(video_id: str) -> str:
    return os.path.join(get_video_dir(video_id), _VOICE_MAP_FILE)


def _read_voice_map(video_id: str) -> dict[str, str]:
    """The cached map, loading it from disk on first use. Caller holds the lock."""
    if video_id in _voice_map_cache:
        return _voice_map_cache[video_id]

    data: dict[str, str] = {}
    path = _voice_map_path(video_id)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                # Drop empty values: they would read as "pinned to nothing"
                # rather than "automatic", which is a different state.
                data = {str(k): str(v) for k, v in loaded.items() if v}
        except Exception as e:
            # A corrupt file must not stop a run; the worst case is that
            # speakers get re-assigned.
            logger.warning(f"[{video_id}] Could not read {_VOICE_MAP_FILE}: {e}")

    _voice_map_cache[video_id] = data
    return data


def _write_voice_map(video_id: str) -> None:
    """Persist the cached map. Caller holds the lock."""
    path = _voice_map_path(video_id)
    data = _voice_map_cache.get(video_id, {})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Written to a temporary file and renamed, so an interrupted write
        # cannot leave a half-file that the next run would read as corrupt.
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning(f"[{video_id}] Could not persist {_VOICE_MAP_FILE}: {e}")


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
    with _voice_map_lock:
        mapping = _read_voice_map(video_id)

        if speaker_id in mapping:
            return mapping[speaker_id]

        if not config.system_voices_support(language):
            # Not fatal — there is a fallback pool, so the run still produces
            # audio — but recorded so the cause is in the log when someone asks
            # why that dub sounds off. Voice cloning is the real fix. Reachable
            # only from a hand-made request now: the UI offers no such language.
            logger.warning(
                f"[{video_id}] No built-in voice is documented for '{language}'; using the "
                f"multilingual pool anyway. Expect mispronunciation — voice cloning covers "
                f"this language."
            )

        pool = config.voices_for_language(language)
        used_voices = set(mapping.values())
        available = [v for v in pool if v not in used_voices]

        if not available:
            # More speakers than voices — reuse, the caller usually has voice
            # cloning enabled for multi-speaker videos anyway.
            available = list(pool)

        voice = random.choice(available)
        mapping[speaker_id] = voice
        _write_voice_map(video_id)
        logger.info(
            f"[{video_id}] Assigned voice '{voice}' to speaker '{speaker_id}' "
            f"(language={language or 'any'}, pool size={len(pool)})"
        )
        return voice


def forget_voice_map(video_id: str) -> None:
    """
    Drop the in-process copy for a video whose files were just deleted.

    `reset_video` clears the directory — which removes `speaker_voices.json` —
    but a cache that outlived the file would both keep serving the old mapping
    and write it back on the next assignment, resurrecting exactly what the user
    asked to have thrown away.
    """
    with _voice_map_lock:
        _voice_map_cache.pop(video_id, None)


def get_speaker_voice_map(video_id: str) -> dict[str, str]:
    """The speaker->voice mapping for a video, as last saved."""
    with _voice_map_lock:
        return dict(_read_voice_map(video_id))


def clear_speaker_voice(video_id: str, speaker_id: str) -> None:
    """
    Drop a speaker's pinned voice, handing them back to automatic assignment.

    Without this, "automatic" would be a one-way door: the picker offers it, and
    once a voice is pinned there would be no way back to the random choice.
    """
    with _voice_map_lock:
        mapping = _read_voice_map(video_id)
        if speaker_id in mapping:
            removed = mapping.pop(speaker_id)
            _write_voice_map(video_id)
            logger.info(f"[{video_id}] Speaker '{speaker_id}' unpinned (was '{removed}')")


def clear_speaker_voices(video_id: str) -> None:
    """
    Drop every pinned voice for a video, sending all speakers back to automatic.

    Used when the target language changes: a voice id is language-specific, so a
    pin made for Chinese is not merely suboptimal for an English dub — the
    service rejects it outright, and it would do so halfway through the run.
    """
    with _voice_map_lock:
        mapping = _read_voice_map(video_id)
        if mapping:
            mapping.clear()
            _write_voice_map(video_id)
            logger.info(f"[{video_id}] Cleared pinned voices (target language changed)")


def set_speaker_voice(video_id: str, speaker_id: str, voice: str) -> None:
    """
    Pin a speaker to a specific voice, replacing whatever was auto-assigned.

    Used when voice cloning is OFF: without it the user has no say at all, and
    `assign_voice_for_speaker` picks at random — so two speakers can swap voices
    between runs and there is no way to ask for a particular one.

    The voice is NOT validated here; the caller checks it against
    `config.voices_for_language` so the error can name the language it failed
    for. Storing it per video, not per segment, is the point: a voice is a
    property of the speaker, and pinning it per line would let one speaker
    change voice halfway through the video.
    """
    with _voice_map_lock:
        mapping = _read_voice_map(video_id)
        mapping[speaker_id] = voice
        _write_voice_map(video_id)
    logger.info(f"[{video_id}] Speaker '{speaker_id}' pinned to voice '{voice}'")


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
    text: str,
    voice: str,
    speech_rate: float | None = None,
    instruction: str | None = None,
    video_id: str | None = None,
) -> bytes:
    """
    One blocking synthesis call.

    `speech_rate` is only sent when the caller provided one: the API's default
    is 1.0 and an unset value keeps the previous behaviour exactly (important
    for the manual re-synthesis and preview paths).

    `instruction` is how a Chinese dialect is requested — Qwen-Audio-TTS has no
    accent parameter. Also omitted when unset, for the same reason as
    `speech_rate`. It is already validated by `config.tts_instruction`; this
    layer only forwards it.

    Usage is captured HERE, not in the retry wrapper: the synthesizer instance
    holds the finished task's response, and only the audio bytes travel back.
    """
    kwargs: dict = {"model": config.TTS_MODEL, "voice": voice}
    if speech_rate is not None:
        kwargs["speech_rate"] = speech_rate
    if instruction:
        kwargs["instruction"] = instruction
    synthesizer = SpeechSynthesizer(**kwargs)
    audio = synthesizer.call(text)
    response = synthesizer.get_response() or {}
    usage_service.record(
        video_id=video_id,
        step="tts",
        model=config.TTS_MODEL,
        detail=voice,
        usage=((response.get("payload") or {}).get("usage")) or {},
    )
    return audio


async def _synthesize_with_retries(
    loop,
    text: str,
    voice: str,
    speech_rate: float | None,
    segment_id: str,
    instruction: str | None = None,
    video_id: str | None = None,
) -> bytes:
    """Synthesize with the configured retry/backoff. Raises on total failure."""
    last_error: Exception | None = None
    for attempt in range(1, config.TTS_MAX_RETRIES + 2):
        try:
            audio = await loop.run_in_executor(
                None, _synthesize_blocking, text, voice, speech_rate, instruction, video_id
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
    instruction: str | None = None,
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
        retry = await _synthesize_with_retries(
            loop, text, voice, corrected, segment_id, instruction, video_id
        )
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


_TAG_RE = re.compile(r"\[[^\[\]]{1,32}\]")
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_for_tts(text: str, segment_id: str) -> str:
    """
    送合成前的最后一道防线：只放行白名单标签。

    上游（omni 标注）不再做任何净化 —— 它产出什么就送什么，这样标签问题在
    enhanced_transcript.json 里可观察。但 TTS 是边界：白名单之外的标记会被
    当正文读出来（比如它自造的 [mad]），这条防线只拦这个。上限沿用原有
    约定（控制类每句 1、富语言每句 2）——多出来的同类标签对合成本来就没
    有增量意义。

    剥除会打日志（带被剥内容），让"防线拦了什么"可追查而不是静默丢失。
    """
    if "[" not in text:
        return text

    kept_control = kept_rich = 0
    stripped: list[str] = []

    def _keep(m: re.Match) -> str:
        nonlocal kept_control, kept_rich
        tag = m.group(0)
        if tag in config.EMOTION_CONTROL_TAGS and kept_control < config.EMOTION_MAX_CONTROL_PER_LINE:
            kept_control += 1
            return tag
        if tag in config.EMOTION_RICH_TAGS and kept_rich < config.EMOTION_MAX_RICH_PER_LINE:
            kept_rich += 1
            return tag
        stripped.append(tag)
        return ""

    out = _TAG_RE.sub(_keep, text)
    if not stripped:
        return text
    cleaned = _WHITESPACE_RE.sub(" ", out).strip()
    logger.warning(
        f"[TTS] {segment_id}: stripped non-whitelisted tag(s) "
        f"{' '.join(stripped)[:200]} — synthesis text now: {cleaned[:80]}"
    )
    return cleaned


async def synthesize_speech(
    video_id: str,
    segment_id: str,
    text: str,
    voice: str | None = None,
    write_registry: bool = True,
    speech_rate: float | None = None,
    target_duration: float | None = None,
    instruction: str | None = None,
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
        instruction: Dialect instruction, already resolved and validated by
            `config.tts_instruction`. None for plain speech. It has to be passed
            to the FITTING step too, or a line that gets re-synthesized at a
            corrected rate would come back in Mandarin while every other line
            stayed in the chosen dialect.

    Returns:
        Path to the saved audio file
    """
    # 入口净化，先于一切：合成与时长拟合都必须用同一份净化后的文本。
    text = _sanitize_for_tts(text, segment_id)
    voice = voice or config.TTS_DEFAULT_VOICE

    logger.info(
        f"Submitting TTS task to DashScope ({config.TTS_MODEL}), Voice: {voice}, "
        f"rate: {speech_rate if speech_rate is not None else 'default'}"
        f"{', instruction: ' + instruction if instruction else ''} for {segment_id}"
    )

    loop = asyncio.get_event_loop()

    # The SDK call is blocking -> run it in the thread pool so the event loop
    # (and other concurrent segment jobs) keep making progress.
    audio = await _synthesize_with_retries(
        loop, text, voice, speech_rate, segment_id, instruction, video_id
    )

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
            instruction,
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
