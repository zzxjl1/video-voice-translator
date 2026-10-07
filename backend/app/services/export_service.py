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
from dataclasses import dataclass
from typing import Callable, Optional

from app import config
from app.models import VideoState, get_state, get_video_dir
from app.services import subtitle_service

logger = logging.getLogger(__name__)

SAMPLE_WIDTH = 2  # int16
MAX_SAMPLE = 32767
MIN_SAMPLE = -32768


def is_available() -> bool:
    """ffmpeg + ffprobe are required for export."""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# Container per subtitle format. Matroska is required for "styled" because MP4
# cannot carry ASS styling at all; "soft" stays MP4 for maximum compatibility.
_CONTAINER_FOR_FORMAT = {"soft": "mp4", "styled": "mkv"}


def get_export_path(video_id: str, container: str = "mp4") -> str:
    """Path of the exported file for a container ("mp4" or "mkv")."""
    name = (
        config.EXPORT_STYLED_FILENAME
        if container == "mkv"
        else config.EXPORT_FILENAME
    )
    return os.path.join(get_video_dir(video_id), name)


def find_export(video_id: str) -> Optional[tuple[str, str]]:
    """
    (path, container) of the current export.

    Only one export is kept at a time (see `_drop_other_exports`), so normally
    there is a single candidate. The newest-by-mtime tiebreak is a safety net
    for the case where an older version left both behind: without it the lookup
    order would silently decide, and the download would serve a stale render
    while the UI claims the new one is ready.
    """
    candidates = []
    for container in ("mp4", "mkv"):
        path = get_export_path(video_id, container)
        if os.path.exists(path):
            try:
                candidates.append((os.path.getmtime(path), path, container))
            except OSError:
                candidates.append((0.0, path, container))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _, path, container = candidates[0]
    return path, container


def _drop_other_exports(video_id: str, keep_container: str) -> None:
    """
    Delete the export in the other container, if any.

    Called only AFTER a successful mux, so a failed export never destroys the
    previous good file. Leaving the stale one would waste a full copy of the
    video and, worse, `find_export` could serve it in place of the fresh render.
    """
    for container in ("mp4", "mkv"):
        if container == keep_container:
            continue
        stale = get_export_path(video_id, container)
        if not os.path.exists(stale):
            continue
        try:
            os.remove(stale)
            logger.info(f"[{video_id}] Removed stale .{container} export")
        except OSError as e:
            logger.warning(f"[{video_id}] Could not remove stale export {stale}: {e}")


def container_for(plan: subtitle_service.ExportPlan) -> str:
    """Which container a plan needs. Falls back to MP4 for a subtitle-free export."""
    if not plan.enabled or plan.format == "off":
        return "mp4"
    return _CONTAINER_FOR_FORMAT.get(plan.format, "mp4")


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

    Rebuilt every time. Caching it would mean recording each clip's identity to
    know when to invalidate — bookkeeping that costs more than the few seconds
    it saves for something the user triggers deliberately rather than in a loop.
    """
    sample_rate = config.EXPORT_SAMPLE_RATE
    segments = [
        seg
        for seg in state.segments
        if seg.translated_text
        and seg.audio_path
        and os.path.exists(seg.audio_path)
        and not getattr(seg, "muted", False)
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
# Subtitles
# ---------------------------------------------------------------------------


@dataclass
class ExportResult:
    """
    What an export produced.

    Returned instead of a bare path because the API layer has to report the
    container and the tracks that actually made it in — and `_prepare_subtitles`
    can skip a track (nothing translated yet), so the result is not simply the
    requested plan echoed back.
    """

    path: str
    container: str
    subtitle_tracks: list[dict]


@dataclass
class SubtitleInput:
    """One subtitle file to embed, plus how the muxer should tag it."""

    track: str          # translated | original | bilingual
    path: str
    codec: str          # "mov_text" for MP4, "ass" for Matroska
    lang: str
    title: str


def _prepare_subtitles(
    video_id: str,
    state: VideoState,
    plan: subtitle_service.ExportPlan,
) -> list[SubtitleInput]:
    """
    Render one file per requested track and describe it for the muxer.

    "soft" and "styled" differ only in the rendered text and the codec — SRT for
    `mov_text`, ASS for `ass`. Track selection, the adapted timeline and the
    style are all shared, so both deliveries produce the same words at the same
    times; only the styling survives in one of them.

    Returns an empty list when subtitles are off, nothing is translated yet, or
    the requested renderer is unavailable. The export then proceeds WITHOUT
    subtitles rather than failing: a missing subtitle track is a far smaller
    problem than losing the whole render.
    """
    if not plan.enabled or plan.format == "off":
        return []

    if plan.format == "burn":
        # Burn-in is a BROWSER job: it needs glyph rasterisation (libass and a
        # CJK font) plus a full video re-encode, and this host has neither the
        # libraries nor the CPU headroom. Reaching this point means the client
        # asked the server for something it cannot do, so fail loudly.
        #
        # The dangerous alternative is falling through to the embedding path:
        # that produces a file with an UNSTYLED subtitle track and no indication
        # anything went wrong, which is exactly the silent wrong output this
        # check exists to prevent.
        raise RuntimeError(
            "Burn-in subtitles are rendered in the browser, not on the server "
            "(the server has no libass or CJK font, and re-encoding is not "
            "affordable here)."
        )

    caps = subtitle_service.capabilities()
    capability_key = "styled" if plan.format == "styled" else "soft"
    capability = caps.get(capability_key, {})
    if not capability.get("available"):
        logger.warning(
            "[%s] Subtitle format %r unavailable (%s); exporting without subtitles",
            video_id,
            plan.format,
            capability.get("reason"),
        )
        return []

    styled = plan.format == "styled"
    codec = "ass" if styled else "mov_text"
    extension = "ass" if styled else "srt"

    subs_dir = os.path.join(get_video_dir(video_id), "subs")
    os.makedirs(subs_dir, exist_ok=True)

    # ASS needs the real frame size: every size in the file is a percentage of
    # it, so a wrong PlayRes renders the text at the wrong scale.
    frame = subtitle_service.probe_video_size(state.file_path) if styled else None

    inputs: list[SubtitleInput] = []
    for track in plan.tracks:
        style = subtitle_service.SubtitleStyle.from_dict(
            {**plan.style.to_dict(), "track": track}
        )
        cues = subtitle_service.build_cues(state.segments, style)
        if not cues:
            logger.info("[%s] No cues for track %r; skipping", video_id, track)
            continue

        text = (
            subtitle_service.to_ass(cues, style, frame[0], frame[1])
            if styled and frame
            else subtitle_service.to_srt(cues, style)
        )
        path = os.path.join(subs_dir, f"{track}.{extension}")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

        # The translation tracks are in the TARGET language; the original track
        # is in whatever the source happened to be, which we do not know.
        source_language = getattr(state, "target_language", None) if track != "original" else None
        inputs.append(
            SubtitleInput(
                track=track,
                path=path,
                codec=codec,
                lang=subtitle_service.language_tag(source_language),
                title=subtitle_service.TRACK_TITLES.get(track, track),
            )
        )

    if inputs:
        logger.info(
            "[%s] Prepared %d subtitle track(s): %s",
            video_id,
            len(inputs),
            ", ".join(f"{i.title}/{i.lang}/{i.codec}" for i in inputs),
        )
    else:
        logger.info("[%s] No subtitle tracks produced", video_id)
    return inputs


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
    subtitle_inputs: Optional[list[SubtitleInput]] = None,
    default_subtitle: int = 0,
) -> None:
    """
    Combine the original video stream + dubbed timeline (+ bgm + subtitles).

    The video stream is always copied. Subtitles are EMBEDDED, never burned:
    embedding is a remux (~40 ms), burning would need a full re-encode plus
    libass and a CJK font, none of which this host has. Burn-in runs in the
    browser instead.
    """
    inputs = subtitle_inputs or []

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

    # Subtitle inputs are appended LAST. The filter_complex above refers to
    # inputs 0/1/2 by index, so inserting them earlier would silently remap the
    # audio to the wrong stream.
    first_subtitle_input = 3 if background_path else 2
    for subtitle_input in inputs:
        cmd += ["-i", subtitle_input.path]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "0:v:0",
        "-map", "[aout]",
    ]
    for index in range(len(inputs)):
        cmd += ["-map", f"{first_subtitle_input + index}:s"]

    cmd += [
        "-c:v", "copy",          # never re-encode the video stream
        "-c:a", "aac",
        "-b:a", "160k",
    ]

    if inputs:
        cmd += ["-c:s", inputs[0].codec]
        for index, subtitle_input in enumerate(inputs):
            # Both tags are needed, and which one a player shows depends on the
            # container: MP4 SILENTLY IGNORES `title` for subtitle streams and
            # only keeps `handler_name`, while Matroska prefers `title`. Setting
            # just one leaves the track unnamed in half the players.
            cmd += [
                f"-metadata:s:s:{index}", f"language={subtitle_input.lang}",
                f"-metadata:s:s:{index}", f"title={subtitle_input.title}",
                f"-metadata:s:s:{index}", f"handler_name={subtitle_input.title}",
            ]
            # Exactly one default track: players that auto-select would
            # otherwise pick the first and ignore the user's choice.
            cmd += [
                f"-disposition:s:{index}",
                "default" if index == default_subtitle else "0",
            ]

    cmd += ["-t", f"{duration:.3f}"]
    if output_path.endswith(".mp4"):
        # Only meaningful for MP4; the Matroska muxer rejects the option.
        cmd += ["-movflags", "+faststart"]
    cmd.append(output_path)

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
    Produce the final dubbed video for a video and return its path.

    Reads the subtitle plan from the video's own state, so the pipeline's
    automatic export and an explicit POST /export produce the same file.

    Emits progress events through `emit` when provided (async callable).
    """
    import asyncio

    if not is_available():
        raise RuntimeError("ffmpeg/ffprobe not found — export requires them on PATH")

    state = get_state(video_id)
    if not state:
        raise RuntimeError("Video not found")

    video_dir = get_video_dir(video_id)

    plan = subtitle_service.ExportPlan.from_dict(
        getattr(state, "subtitle_export", None)
    )
    container = container_for(plan)

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

    # Subtitles are rendered before the mux so a failure here cannot leave a
    # half-written output file behind.
    subtitle_inputs = await loop.run_in_executor(
        None, lambda: _prepare_subtitles(video_id, state, plan)
    )

    # Only tracks that actually produced cues became files, so the requested
    # default has to be clamped into the range that exists — otherwise the
    # disposition would point at a stream index past the end.
    requested_default = (
        plan.tracks.index(plan.default_track) if plan.default_track in plan.tracks else 0
    )
    default_index = min(requested_default, max(0, len(subtitle_inputs) - 1))

    output_path = get_export_path(video_id, container)
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
            subtitle_inputs,
            default_index,
        ),
    )

    # The mux succeeded, so the previous export (a different container) is now
    # stale. Removed after the fact on purpose: a failure above must not destroy
    # a good file the user can still download.
    _drop_other_exports(video_id, container)

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    tracks = [
        {
            "track": subtitle_input.track,
            "title": subtitle_input.title,
            "language": subtitle_input.lang,
            "codec": subtitle_input.codec,
            "default": index == default_index,
        }
        for index, subtitle_input in enumerate(subtitle_inputs)
    ]
    logger.info(
        f"[{video_id}] Export complete: {output_path} ({size_mb:.1f} MB, "
        f"{len(tracks)} subtitle track(s), container={container})"
    )
    await _emit(
        {
            "phase": "export",
            "status": "done",
            "url": f"/api/videos/{video_id}/export/download",
            "size_mb": round(size_mb, 1),
            "container": container,
            "subtitle_tracks": tracks,
        }
    )
    return ExportResult(path=output_path, container=container, subtitle_tracks=tracks)
