"""
Export service: assemble the dubbed audio and mux it back into the video.

Why it is built this way
------------------------
The server may be a 2 GB / single-core box with no GPU, so the export must be
cheap:

* The **video stream is copied**, never re-encoded (`-c:v copy`). Only the
  audio is rebuilt, which is a small fraction of the work.
* The dubbed timeline is assembled in Python (each TTS clip written at its
  original `start_time`), so we need one short ffmpeg decode per segment and
  a single mux at the end — no giant `filter_complex` with hundreds of inputs.
* numpy is used for the mixing when present; a stdlib fallback keeps the
  module working without it.
"""
import array
import logging
import os
import shutil
import subprocess
import wave
from typing import Callable, Optional

from app import config
from app.models import VideoState, get_state, get_video_dir

logger = logging.getLogger(__name__)

SAMPLE_WIDTH = 2  # int16
MAX_SAMPLE = 32767
MIN_SAMPLE = -32768


def is_available() -> bool:
    """ffmpeg + ffprobe are required for export."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def get_export_path(video_id: str) -> str:
    return os.path.join(get_video_dir(video_id), config.EXPORT_FILENAME)


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def _probe_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return 0.0


def _has_audio_stream(path: str) -> bool:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "a",
            "-show_entries", "stream=index",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def _decode_to_pcm(path: str, sample_rate: int) -> bytes:
    """Decode any audio file to raw mono s16le PCM."""
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", path,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ac", "1",
            "-ar", str(sample_rate),
            "-",
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to decode {path}: "
            f"{result.stderr.decode('utf-8', errors='replace')[-400:]}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Timeline assembly
# ---------------------------------------------------------------------------

def _mix_into_timeline_numpy(
    timeline, pcm_bytes: bytes, start_sample: int
) -> None:
    import numpy as np  # local import: only needed when available

    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.int32)
    total = len(timeline)
    end = min(total, start_sample + len(samples))
    if end <= start_sample:
        return
    timeline[start_sample:end] += samples[: end - start_sample]


def _mix_into_timeline_stdlib(
    timeline: array.array, pcm_bytes: bytes, start_sample: int
) -> None:
    samples = array.array("h")
    samples.frombytes(pcm_bytes[: len(pcm_bytes) // SAMPLE_WIDTH * SAMPLE_WIDTH])

    total = len(timeline)
    for index, value in enumerate(samples):
        position = start_sample + index
        if position >= total:
            break
        mixed = timeline[position] + value
        timeline[position] = (
            MAX_SAMPLE if mixed > MAX_SAMPLE else MIN_SAMPLE if mixed < MIN_SAMPLE else mixed
        )


def build_dubbed_timeline(
    video_id: str,
    state: VideoState,
    duration: float,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Optional[str]:
    """
    Write every synthesized segment at its original timestamp into a single
    WAV file. Returns the WAV path, or None when nothing could be placed.
    """
    sample_rate = config.EXPORT_SAMPLE_RATE
    segments = [
        seg
        for seg in state.segments
        if seg.translated_text and seg.audio_path and os.path.exists(seg.audio_path)
    ]
    if not segments:
        logger.warning(f"[{video_id}] No synthesized segments available for export")
        return None

    total_samples = int(duration * sample_rate) + sample_rate  # +1s tail
    if total_samples <= 0:
        raise RuntimeError("Cannot determine video duration for export")

    try:
        import numpy as np
    except ImportError:
        np = None  # type: ignore[assignment]
        logger.warning("numpy not available, using slower stdlib audio mixing")

    use_numpy = np is not None

    if use_numpy:
        timeline = np.zeros(total_samples, dtype=np.int32)
    else:
        timeline = array.array("h", bytes(total_samples * SAMPLE_WIDTH))

    placed = 0
    for index, seg in enumerate(segments):
        try:
            pcm = _decode_to_pcm(seg.audio_path, sample_rate)
        except Exception as e:
            logger.warning(f"[{video_id}] Skipping segment {seg.id}: {e}")
            continue

        start_sample = int(max(0.0, seg.start_time) * sample_rate)
        if use_numpy:
            _mix_into_timeline_numpy(timeline, pcm, start_sample)
        else:
            _mix_into_timeline_stdlib(timeline, pcm, start_sample)
        placed += 1

        if progress and (index % 5 == 0 or index == len(segments) - 1):
            try:
                progress(index + 1, len(segments))
            except Exception:
                pass

    if placed == 0:
        return None

    if use_numpy:
        pcm_bytes = np.clip(timeline, MIN_SAMPLE, MAX_SAMPLE).astype(np.int16).tobytes()
    else:
        pcm_bytes = timeline.tobytes()

    timeline_path = os.path.join(get_video_dir(video_id), "dubbed_timeline.wav")

    with wave.open(timeline_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm_bytes)

    logger.info(
        f"[{video_id}] Dubbed timeline built: {placed}/{len(segments)} segments, "
        f"{duration:.1f}s @ {sample_rate}Hz -> {timeline_path}"
    )
    return timeline_path


# ---------------------------------------------------------------------------
# Mux
# ---------------------------------------------------------------------------

def _mux(
    video_id: str,
    video_path: str,
    timeline_path: str,
    background_path: Optional[str],
    has_source_audio: bool,
    output_path: str,
    duration: float,
) -> None:
    """Combine original video stream + dubbed timeline (+ bgm) into an MP4."""
    cmd = ["ffmpeg", "-y", "-v", "warning", "-i", video_path, "-i", timeline_path]

    if background_path:
        cmd += ["-i", background_path]
        filter_complex = (
            f"[1:a]volume={config.EXPORT_TTS_GAIN},aresample={config.EXPORT_SAMPLE_RATE}[tts];"
            f"[2:a]volume={config.EXPORT_BGM_GAIN},aresample={config.EXPORT_SAMPLE_RATE}[bg];"
            f"[tts][bg]amix=inputs=2:duration=longest:normalize=0,"
            f"alimiter=limit=0.97[aout]"
        )
    elif has_source_audio:
        # No separated stems: keep the original track but duck it hard.
        filter_complex = (
            f"[0:a]volume={config.EXPORT_ORIGINAL_AUDIO_GAIN}[orig];"
            f"[1:a]volume={config.EXPORT_TTS_GAIN},aresample={config.EXPORT_SAMPLE_RATE}[tts];"
            f"[orig][tts]amix=inputs=2:duration=first:normalize=0,"
            f"alimiter=limit=0.97[aout]"
        )
    else:
        filter_complex = f"[1:a]volume={config.EXPORT_TTS_GAIN},alimiter=limit=0.97[aout]"

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "[aout]",
        "-c:v", "copy",          # never re-encode the video stream
        "-c:a", "aac",
        "-b:a", "160k",
        "-t", f"{duration:.3f}",
        "-movflags", "+faststart",
        output_path,
    ]

    logger.info(f"[{video_id}] Muxing: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg mux failed: {result.stderr[-800:] if result.stderr else 'unknown error'}"
        )


async def export_video(
    video_id: str,
    emit: Optional[Callable] = None,
) -> str:
    """
    Produce the final dubbed MP4 for a video and return its path.

    Emits progress events through `emit` when provided (async callable).
    """
    import asyncio

    if not is_available():
        raise RuntimeError("ffmpeg/ffprobe not found — export requires them on PATH")
    if not config.EXPORT_ENABLED:
        raise RuntimeError("Export is disabled (EXPORT_ENABLED=false)")

    state = get_state(video_id)
    if not state:
        raise RuntimeError("Video not found")

    video_dir = get_video_dir(video_id)

    async def _emit(event: dict) -> None:
        if emit:
            await emit(event)

    await _emit({"phase": "export", "status": "started"})

    duration = await asyncio.get_event_loop().run_in_executor(
        None, _probe_duration, state.file_path
    )
    if duration <= 0:
        raise RuntimeError("Failed to probe the video duration")

    has_source_audio = await asyncio.get_event_loop().run_in_executor(
        None, _has_audio_stream, state.file_path
    )

    # Background stem is only meaningful if separation actually ran.
    background_path = os.path.join(video_dir, "background.wav")
    if not os.path.exists(background_path):
        background_path = None

    loop = asyncio.get_event_loop()
    progress_queue: asyncio.Queue = asyncio.Queue()

    def on_progress(current: int, total: int) -> None:
        loop.call_soon_threadsafe(progress_queue.put_nowait, (current, total))

    timeline_task = asyncio.ensure_future(
        loop.run_in_executor(
            None,
            lambda: build_dubbed_timeline(video_id, state, duration, progress=on_progress),
        )
    )

    while not timeline_task.done():
        try:
            current, total = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
            await _emit({"phase": "export", "status": "mixing", "current": current, "total": total})
        except asyncio.TimeoutError:
            continue

    timeline_path = timeline_task.result()
    if not timeline_path:
        raise RuntimeError("No synthesized audio to export — run TTS first")

    await _emit({"phase": "export", "status": "muxing"})

    output_path = get_export_path(video_id)
    await loop.run_in_executor(
        None,
        lambda: _mux(
            video_id,
            state.file_path,
            timeline_path,
            background_path,
            has_source_audio,
            output_path,
            duration,
        ),
    )

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info(f"[{video_id}] Export complete: {output_path} ({size_mb:.1f} MB)")
    await _emit(
        {
            "phase": "export",
            "status": "done",
            "url": f"/api/videos/{video_id}/export/download",
            "size_mb": round(size_mb, 1),
        }
    )
    return output_path
