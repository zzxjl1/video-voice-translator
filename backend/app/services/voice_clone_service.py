"""
Voice Cloning Service using DashScope CosyVoice VoiceEnrollment.

Flow:
1. For each speaker, collect all vocal segments sorted by time
2. Concatenate them with 2s silence gaps into a sample audio (stop when max duration reached)
3. Upload to public URL, call DashScope voice enrollment API
4. Poll until voice is ready (status=OK)
5. Return voice_id for use in TTS synthesis
"""
import asyncio
import json
import logging
import os
import time
from typing import Optional

import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService, SpeechSynthesizer

from app import config
from app.models import Segment, get_video_dir

logger = logging.getLogger(__name__)

dashscope.api_key = config.DASHSCOPE_API_KEY

# In-memory cache: {video_id: {speaker_id: voice_id}}
_cloned_voice_map: dict[str, dict[str, str]] = {}

# Target model for voice cloning — must match TTS model
CLONE_TARGET_MODEL = config.TTS_MODEL  # cosyvoice-v3-flash

# Sample constraints
SAMPLE_MAX_DURATION = 30.0   # stop appending after this
SAMPLE_MIN_DURATION = 10.0    # pad silence if below
GAP_DURATION = 1.0           # silence gap between segments


def _select_segments_for_speaker(
    segments: list[Segment],
    speaker_id: str,
) -> list[Segment]:
    """
    Select segments for a speaker: take all valid segments in time order,
    stop when accumulated duration (voice + gaps) would exceed max.
    """
    all_speaker_segs = [s for s in segments if s.speaker_id == speaker_id]
    speaker_segs = [
        s for s in all_speaker_segs if (s.end_time - s.start_time) >= 0.3
    ]
    speaker_segs.sort(key=lambda s: s.start_time)
    skipped = len(all_speaker_segs) - len(speaker_segs)

    if not speaker_segs:
        logger.warning(
            f"[SegSelect] {speaker_id}: no valid segments "
            f"(total={len(all_speaker_segs)}, all < 0.3s)"
        )
        return []

    total_available = sum(s.end_time - s.start_time for s in speaker_segs)
    logger.info(
        f"[SegSelect] {speaker_id}: {len(speaker_segs)} valid segments "
        f"({skipped} skipped < 0.3s), total voice={total_available:.1f}s"
    )

    # Log all candidates
    for s in speaker_segs:
        dur = s.end_time - s.start_time
        logger.info(
            f"[SegSelect]   {s.id}  [{s.start_time:.2f}-{s.end_time:.2f}]  "
            f"dur={dur:.2f}s  text=\"{(s.text or '')[:60]}\""
        )

    # Greedily pick segments in time order until max duration
    selected: list[Segment] = []
    accumulated = 0.0  # voice + gaps total

    for s in speaker_segs:
        dur = s.end_time - s.start_time
        gap = GAP_DURATION if selected else 0.0  # no gap before first segment
        needed = gap + dur

        if accumulated + needed > SAMPLE_MAX_DURATION:
            # Would exceed max — stop
            logger.info(
                f"[SegSelect]   STOP at {s.id}: adding {needed:.2f}s "
                f"would exceed max ({accumulated:.1f}+{needed:.1f} > {SAMPLE_MAX_DURATION})"
            )
            break

        selected.append(s)
        accumulated += needed
        logger.info(
            f"[SegSelect]   ✓ {s.id}  dur={dur:.2f}s  "
            f"cumulative={accumulated:.1f}s (voice+gaps)"
        )

    voice_dur = sum(s.end_time - s.start_time for s in selected)
    n_gaps = max(0, len(selected) - 1)
    logger.info(
        f"[SegSelect] Final: {len(selected)} segments, "
        f"voice={voice_dur:.1f}s + {n_gaps}×{GAP_DURATION}s gaps = "
        f"{voice_dur + n_gaps * GAP_DURATION:.1f}s total"
    )

    return selected


def _ffmpeg_build_sample(
    vocals_path: str,
    selected: list[Segment],
    output_path: str,
) -> float:
    """
    Extract segments from vocals, concatenate with 2s silence gaps between them.
    If total < SAMPLE_MIN_DURATION, loop (repeat) segments until minimum is met.
    Returns total output duration.
    """
    import subprocess

    voice_dur = sum(s.end_time - s.start_time for s in selected)
    n_gaps = max(0, len(selected) - 1)
    gap_total = n_gaps * GAP_DURATION
    one_pass_dur = voice_dur + gap_total
    need_loop = one_pass_dur < SAMPLE_MIN_DURATION

    logger.info(
        f"[FFmpeg] Building sample: {len(selected)} segments, "
        f"voice={voice_dur:.2f}s, gaps={n_gaps}×{GAP_DURATION}s={gap_total:.1f}s, "
        f"one_pass={one_pass_dur:.2f}s, need_loop={need_loop}"
    )
    logger.info(f"[FFmpeg] Input: {vocals_path}")
    logger.info(f"[FFmpeg] Output: {output_path}")

    # Build the sequence of segments to concat.
    # If one pass is not enough, repeat the segment list until we exceed min duration.
    play_list: list[Segment] = []
    accumulated = 0.0

    if need_loop:
        loop_round = 0
        while accumulated < SAMPLE_MIN_DURATION:
            loop_round += 1
            for seg in selected:
                dur = seg.end_time - seg.start_time
                gap = GAP_DURATION if play_list else 0.0
                play_list.append(seg)
                accumulated += gap + dur
                if accumulated >= SAMPLE_MIN_DURATION:
                    break
        logger.info(
            f"[FFmpeg] Looped {loop_round} round(s) → {len(play_list)} entries, "
            f"accumulated={accumulated:.2f}s (min={SAMPLE_MIN_DURATION}s)"
        )
    else:
        play_list = list(selected)
        accumulated = one_pass_dur

    # Now build ffmpeg filter_complex from play_list
    filter_parts = []
    stream_labels = []
    stream_idx = 0

    for i, seg in enumerate(play_list):
        dur = seg.end_time - seg.start_time
        label = f"a{stream_idx}"
        logger.info(
            f"[FFmpeg]   [{stream_idx}] trim {seg.id}: "
            f"[{seg.start_time:.3f}-{seg.end_time:.3f}] dur={dur:.3f}s  "
            f"\"{(seg.text or '')[:40]}\""
        )
        filter_parts.append(
            f"[0:a]atrim=start={seg.start_time}:end={seg.end_time},"
            f"asetpts=PTS-STARTPTS[{label}];"
        )
        stream_labels.append(f"[{label}]")
        stream_idx += 1

        # Insert 2s silence gap after each segment except the last
        if i < len(play_list) - 1:
            gap_label = f"g{stream_idx}"
            filter_parts.append(
                f"aevalsrc=0:d={GAP_DURATION}:s=24000:c=mono[{gap_label}];"
            )
            stream_labels.append(f"[{gap_label}]")
            stream_idx += 1
            logger.info(
                f"[FFmpeg]   [{stream_idx-1}] gap: {GAP_DURATION}s silence"
            )

    concat_n = len(stream_labels)
    concat_inputs = "".join(stream_labels)
    # Concat then normalize loudness (EBU R128 → -16 LUFS, loud & clear for DashScope)
    filter_parts.append(
        f"{concat_inputs}concat=n={concat_n}:v=0:a=1[raw];"
        f"[raw]loudnorm=I=-16:TP=-1.5:LRA=11[out]"
    )
    filter_complex = "".join(filter_parts)

    logger.info(f"[FFmpeg] filter_complex:\n{filter_complex}")

    cmd = [
        "ffmpeg", "-y",
        "-i", vocals_path,
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-ar", "24000", "-ac", "1",
        "-acodec", "pcm_s16le", "-sample_fmt", "s16",
        output_path,
    ]
    logger.info(f"[FFmpeg] cmd: {' '.join(cmd)}")

    result = subprocess.run(cmd, check=True, capture_output=True)
    if result.stderr:
        logger.debug(
            f"[FFmpeg] stderr: "
            f"{result.stderr.decode('utf-8', errors='replace')[-500:]}"
        )

    if os.path.exists(output_path):
        file_size = os.path.getsize(output_path)
        logger.info(
            f"[FFmpeg] Done: output≈{accumulated:.2f}s, "
            f"file_size={file_size/1024:.1f}KB"
        )
    else:
        logger.warning(f"[FFmpeg] Output file not found: {output_path}")

    return accumulated


async def clone_voice_for_speaker(
    video_id: str,
    speaker_id: str,
    segments: list[Segment],
    server_url_base: str,
) -> str:
    """
    Clone voice for a speaker:
    1. Use LLM to intelligently select the best audio segments
    2. Extract and concatenate sample audio from vocals
    3. Serve it via public URL
    4. Call DashScope voice enrollment
    5. Poll until ready
    6. Return voice_id

    The cloned voice_id is cached and persisted to disk.
    """
    logger.info(
        f"[Clone] ========== Start voice cloning for {speaker_id} (video={video_id}) =========="
    )
    logger.info(f"[Clone] Total segments in video: {len(segments)}")

    # Check cache first
    if video_id in _cloned_voice_map and speaker_id in _cloned_voice_map[video_id]:
        voice_id = _cloned_voice_map[video_id][speaker_id]
        logger.info(f"[Clone] Hit memory cache for {speaker_id}: {voice_id}")
        return voice_id

    # Check disk cache
    video_dir = get_video_dir(video_id)
    clone_dir = os.path.join(video_dir, "voice_clone")
    clone_map_path = os.path.join(clone_dir, "voice_clone_map.json")
    if os.path.exists(clone_map_path):
        try:
            with open(clone_map_path, "r") as f:
                disk_map = json.load(f)
            logger.info(f"[Clone] Disk cache found: {clone_map_path}, entries={list(disk_map.keys())}")
            if speaker_id in disk_map:
                voice_id = disk_map[speaker_id]
                logger.info(f"[Clone] Found {speaker_id} in disk cache: {voice_id}, verifying...")
                # Verify voice is still valid
                try:
                    service = VoiceEnrollmentService()
                    info = service.query_voice(voice_id=voice_id)
                    status = info.get("status", "UNKNOWN")
                    logger.info(f"[Clone] Voice {voice_id} status={status}")
                    if status == "OK":
                        if video_id not in _cloned_voice_map:
                            _cloned_voice_map[video_id] = {}
                        _cloned_voice_map[video_id][speaker_id] = voice_id
                        logger.info(f"[Clone] Restored cloned voice from disk for {speaker_id}: {voice_id}")
                        return voice_id
                    else:
                        logger.warning(f"[Clone] Cached voice {voice_id} status={status}, re-cloning...")
                except Exception as e:
                    logger.warning(f"[Clone] Failed to verify cached voice {voice_id}: {e}, re-cloning...")
        except Exception as e:
            logger.warning(f"[Clone] Failed to read disk cache: {e}")
            pass

    # Step 1: Select segments and build sample audio
    vocals_path = os.path.join(video_dir, "vocals.wav")
    if not os.path.exists(vocals_path):
        vocals_path = os.path.join(video_dir, "extracted_audio.wav")
    if not os.path.exists(vocals_path):
        raise RuntimeError("No audio file found for voice cloning")

    logger.info(f"[Clone] Step 1: Source audio = {vocals_path}")
    speaker_clone_dir = os.path.join(video_dir, "voice_clone", speaker_id)
    os.makedirs(speaker_clone_dir, exist_ok=True)
    sample_path = os.path.join(speaker_clone_dir, "sample.wav")
    logger.info(f"[Clone] Output sample path = {sample_path}")

    # Select segments (greedy, time-ordered, max 20s with 2s gaps)
    logger.info(f"[Clone] Step 1a: Selecting segments for {speaker_id}...")
    selected = _select_segments_for_speaker(segments, speaker_id)
    if not selected:
        raise RuntimeError(f"No suitable segments found for speaker {speaker_id}")

    total_voice = sum(s.end_time - s.start_time for s in selected)
    logger.info(
        f"[Clone] Step 1a result: {len(selected)} segments, "
        f"voice={total_voice:.2f}s"
    )

    if total_voice < 1.0:
        raise RuntimeError(
            f"Insufficient audio for speaker {speaker_id}: only {total_voice:.1f}s (need ≥1s)"
        )

    # Build sample with ffmpeg (segments + 2s gaps)
    logger.info(f"[Clone] Step 1b: Building sample with ffmpeg...")
    loop = asyncio.get_event_loop()
    sample_dur = await loop.run_in_executor(
        None,
        lambda: _ffmpeg_build_sample(vocals_path, selected, sample_path),
    )
    logger.info(f"[Clone] Step 1b done: sample ready, duration={sample_dur:.2f}s")

    # Step 2: Public URL for the sample
    # Serve via our API endpoint
    sample_url = f"{server_url_base}/api/videos/{video_id}/voice-sample/{speaker_id}"
    logger.info(f"[Clone] Step 2: Sample URL = {sample_url}")

    # Step 3: Call DashScope voice enrollment
    safe_video_prefix = video_id[:8]
    safe_speaker = speaker_id.replace(" ", "").lower()[:6]
    prefix = f"v{safe_video_prefix}{safe_speaker}"
    # Prefix: only lowercase letters and digits, max 10 chars
    prefix = "".join(c for c in prefix if c.isalnum())[:10]

    logger.info(
        f"[Clone] Step 3: DashScope voice enrollment — "
        f"model={CLONE_TARGET_MODEL}, prefix={prefix}, url={sample_url}"
    )

    def _create_voice():
        service = VoiceEnrollmentService()
        voice_id = service.create_voice(
            target_model=CLONE_TARGET_MODEL,
            prefix=prefix,
            url=sample_url,
        )
        return voice_id

    # Retry create_voice when DashScope reports download issues
    max_create_attempts = 3
    voice_id = None
    for attempt in range(1, max_create_attempts + 1):
        try:
            voice_id = await loop.run_in_executor(None, _create_voice)
            logger.info(
                f"[Clone] Step 3 done: enrollment submitted, voice_id={voice_id} (attempt {attempt}/{max_create_attempts})"
            )
            break
        except Exception as e:
            msg = str(e)
            transient = any(
                keyword in msg
                for keyword in [
                    "InputDownloadFailed",
                    "download audio failed",
                    "HTTP 415",
                ]
            )
            logger.warning(
                f"[Clone] create_voice failed on attempt {attempt}/{max_create_attempts}: {msg}"
            )
            if attempt == max_create_attempts or not transient:
                raise
            time.sleep(3)

    if voice_id is None:
        raise RuntimeError(f"Failed to create voice for {speaker_id} after retries")

    # Step 4: Poll until voice is ready
    max_attempts = 30
    poll_interval = 10
    logger.info(
        f"[Clone] Step 4: Polling voice status (max {max_attempts} attempts, "
        f"interval {poll_interval}s, timeout {max_attempts * poll_interval}s)"
    )

    def _poll_voice():
        service = VoiceEnrollmentService()
        for attempt in range(max_attempts):
            try:
                info = service.query_voice(voice_id=voice_id)
                status = info.get("status", "UNKNOWN")
                logger.info(
                    f"[Clone] Poll {attempt+1}/{max_attempts}: voice={voice_id}, status={status}"
                )
                if status == "OK":
                    return True
                if status == "UNDEPLOYED":
                    raise RuntimeError(
                        f"Voice clone failed (UNDEPLOYED) for {speaker_id}. "
                        "Audio quality may be insufficient."
                    )
                time.sleep(poll_interval)
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"[Clone] Poll error: {e}")
                time.sleep(poll_interval)
        raise RuntimeError(
            f"Voice clone timed out for {speaker_id} after {max_attempts * poll_interval}s"
        )

    await loop.run_in_executor(None, _poll_voice)
    logger.info(f"[Clone] Step 4 done: voice ready! voice_id={voice_id}")

    # Step 5: Cache and persist
    logger.info(f"[Clone] Step 5: Saving voice_id to cache and disk")
    if video_id not in _cloned_voice_map:
        _cloned_voice_map[video_id] = {}
    _cloned_voice_map[video_id][speaker_id] = voice_id

    # Save to disk
    clone_dir = os.path.join(video_dir, "voice_clone")
    os.makedirs(clone_dir, exist_ok=True)
    clone_map_path = os.path.join(clone_dir, "voice_clone_map.json")
    disk_map = {}
    if os.path.exists(clone_map_path):
        try:
            with open(clone_map_path, "r") as f:
                disk_map = json.load(f)
        except Exception:
            pass
    disk_map[speaker_id] = voice_id
    with open(clone_map_path, "w") as f:
        json.dump(disk_map, f, indent=2)
    logger.info(f"[Clone] Saved to {clone_map_path}: {disk_map}")
    logger.info(
        f"[Clone] ========== Voice cloning COMPLETE for {speaker_id}: "
        f"voice_id={voice_id} =========="
    )

    return voice_id


def get_cloned_voice(video_id: str, speaker_id: str) -> Optional[str]:
    """Get cached cloned voice_id for a speaker, or None."""
    if video_id in _cloned_voice_map:
        return _cloned_voice_map[video_id].get(speaker_id)

    # Try disk
    video_dir = get_video_dir(video_id)
    clone_map_path = os.path.join(video_dir, "voice_clone", "voice_clone_map.json")
    if os.path.exists(clone_map_path):
        try:
            with open(clone_map_path, "r") as f:
                disk_map = json.load(f)
            if speaker_id in disk_map:
                if video_id not in _cloned_voice_map:
                    _cloned_voice_map[video_id] = {}
                _cloned_voice_map[video_id][speaker_id] = disk_map[speaker_id]
                return disk_map[speaker_id]
        except Exception:
            pass
    return None


async def preview_cloned_voice(
    video_id: str,
    speaker_id: str,
    target_language: str = "Chinese",
) -> Optional[str]:
    """
    Generate a short preview TTS using the cloned voice.
    Returns path to the preview audio file, or None if no cloned voice.
    """
    voice_id = get_cloned_voice(video_id, speaker_id)
    if not voice_id:
        return None

    # Multi-language preview text
    preview_texts = {
        "Chinese": "你好，这是克隆后的声音效果展示。",
        "English": "Hello, this is a demonstration of the cloned voice.",
        "Japanese": "こんにちは、これはクローンされた音声のデモです。",
        "Korean": "안녕하세요, 이것은 복제된 음성의 데모입니다.",
        "French": "Bonjour, ceci est une démonstration de la voix clonée.",
        "German": "Hallo, dies ist eine Demonstration der geklonten Stimme.",
        "Spanish": "Hola, esta es una demostración de la voz clonada.",
    }
    text = preview_texts.get(target_language, preview_texts["English"])

    video_dir = get_video_dir(video_id)
    speaker_clone_dir = os.path.join(video_dir, "voice_clone", speaker_id)
    os.makedirs(speaker_clone_dir, exist_ok=True)
    preview_path = os.path.join(speaker_clone_dir, "preview.mp3")

    loop = asyncio.get_event_loop()

    def _synthesize():
        synthesizer = SpeechSynthesizer(
            model=CLONE_TARGET_MODEL,
            voice=voice_id,
        )
        audio = synthesizer.call(text)
        if not audio:
            raise RuntimeError("Preview TTS returned empty audio")
        with open(preview_path, "wb") as f:
            f.write(audio)
        return preview_path

    result = await loop.run_in_executor(None, _synthesize)
    logger.info(f"[{video_id}] Preview audio generated for {speaker_id}: {result}")
    return result
