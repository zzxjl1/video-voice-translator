"""
Voice Cloning Service using DashScope voice enrollment.

Flow:
1. For each speaker, collect all vocal segments sorted by time
2. Concatenate them with silence gaps into a sample audio (stop when max duration reached)
3. Upload to public URL, call DashScope voice enrollment API
4. Poll until voice is ready (status=OK)
5. Persist the voice_id *and the model it was enrolled for*

Voice ids are model-scoped
--------------------------
The official docs are explicit: "音色在创建时通过 target_model 绑定到特定的语音
合成模型，不能跨模型使用" — a voice created for one model cannot be synthesized
with another. Reusing one raises `InvalidParameter` / `Engine error [411]`, and
because `tts_results.json` caches per-segment audio, a model switch could
otherwise keep shipping voice ids that no longer work.

`voice_clone_map.json` therefore records `target_model` next to every voice id:

    {"Speaker 1": {"voice_id": "...", "target_model": "qwen-audio-3.1-tts-flash",
                   "created_at": 1759536000}}

Entries written by older versions are plain strings
(`{"Speaker 1": "voice-id"}`); those carry no model, so they are treated as
invalid and re-cloned. That costs one extra enrollment per speaker after an
upgrade — voice creation is free for Qwen-Audio-TTS — and is far better than
silently producing broken audio.
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Callable, Optional

import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService, SpeechSynthesizer

from app import config
from app.models import Segment, get_state, get_video_dir

logger = logging.getLogger(__name__)

dashscope.api_key = config.DASHSCOPE_API_KEY

# In-memory cache: {video_id: {speaker_id: voice_id}}
#
# Only ever populated with voices whose target_model matches the current TTS
# model, so a model switch cannot leak a stale voice id into synthesis.
_cloned_voice_map: dict[str, dict[str, str]] = {}

CLONE_MAP_FILENAME = "voice_clone_map.json"


def clone_target_model() -> str:
    """
    The model cloned voices are enrolled against.

    Always follows `config.TTS_MODEL` so the enrollment model and the synthesis
    model can never drift apart (they must match exactly, or synthesis fails).
    """
    return config.TTS_MODEL


def _clone_map_path(video_dir: str) -> str:
    return os.path.join(video_dir, "voice_clone", CLONE_MAP_FILENAME)


def load_clone_map(video_dir: str) -> dict[str, dict]:
    """
    Read `voice_clone_map.json`, normalising the legacy shape.

    Current: `{"Speaker 1": {"voice_id": ..., "target_model": ..., "created_at": ...}}`
    Legacy:  `{"Speaker 1": "voice-id"}`

    Legacy entries get `target_model = None`, which `_cached_voice_for_model`
    treats as invalid so they are re-cloned rather than reused against an
    unknown model.
    """
    path = _clone_map_path(video_dir)
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning(f"[Clone] Failed to read {path}: {e}")
        return {}

    if not isinstance(raw, dict):
        logger.warning(f"[Clone] Unexpected clone map shape in {path}, ignoring")
        return {}

    normalised: dict[str, dict] = {}
    for speaker_id, entry in raw.items():
        if isinstance(entry, str):
            normalised[speaker_id] = {"voice_id": entry, "target_model": None}
        elif isinstance(entry, dict) and entry.get("voice_id"):
            normalised[speaker_id] = {
                "voice_id": entry["voice_id"],
                "target_model": entry.get("target_model"),
                "created_at": entry.get("created_at"),
            }
        else:
            logger.warning(f"[Clone] Skipping malformed clone entry for {speaker_id}")
    return normalised


def save_clone_entry(
    video_dir: str, speaker_id: str, voice_id: str, target_model: str
) -> None:
    """Persist one speaker -> voice_id mapping, tagged with its target model."""
    path = _clone_map_path(video_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                raw = loaded
        except Exception as e:
            # 【不写回】：写回会用"只有这一位说话人"的新表覆盖掉整份克隆记录，
            # 其它说话人随后会被认为"没克隆过"而重新走一遍 enrollment ——
            # 那是要花配额（1000 个/账号）且不可退还的操作。
            logger.error(
                f"[{video_id}] voice_clones.json is corrupt ({e}); refusing to "
                f"overwrite it. Speaker {speaker_id}'s voice was NOT persisted."
            )
            return

    raw[speaker_id] = {
        "voice_id": voice_id,
        "target_model": target_model,
        "created_at": int(time.time()),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    logger.info(
        f"[Clone] Saved {speaker_id} -> {voice_id} "
        f"(target_model={target_model}) to {path}"
    )


def _cached_voice_for_model(video_dir: str, speaker_id: str) -> Optional[str]:
    """
    Cached voice id for a speaker, but ONLY when it was created for the model
    currently configured. Returns None otherwise, which triggers a re-clone.

    Cheap on purpose (one small file read, no network): this runs once per
    segment via `get_cloned_voice`.
    """
    entry = load_clone_map(video_dir).get(speaker_id)
    if not entry:
        return None

    voice_id = entry.get("voice_id")
    entry_model = entry.get("target_model")
    current_model = clone_target_model()

    if not entry_model:
        logger.info(
            f"[Clone] Cached voice for {speaker_id} predates model tracking; "
            f"re-cloning against '{current_model}'."
        )
        return None

    if entry_model != current_model:
        logger.info(
            f"[Clone] Cached voice for {speaker_id} was created for "
            f"'{entry_model}' but the TTS model is now '{current_model}'; "
            "re-cloning (cloned voices cannot be used across models)."
        )
        return None

    return voice_id


# Sample constraints, aligned with the official 声音复刻 docs for
# Qwen-Audio-TTS (help.aliyun.com 声音复刻):
#   - 时长推荐 10~20 秒，硬上限 60 秒 → cap at the recommended 20, not 30:
#     more than the recommended window buys nothing and drifts toward the
#     hard limit.
#   - 其余部分仅允许短暂停顿 ≤ 2 秒 → the 1s gaps between segments are
#     within that; do not widen them.
#   - 必须包含至少 5 秒连续清晰的朗读内容 → drives the LONGEST-FIRST
#     selection in `_select_segments_for_speaker` below.
#   - 格式 WAV/MP3/M4A、≤10MB、采样率 ≥16kHz → met by construction: the
#     ffmpeg output stage resamples EVERYTHING to 24kHz mono pcm_s16le
#     (`-ar 24000 -ac 1`), so even an 8kHz source produces a compliant
#     sample. Verified against real enrollments: 24000 Hz / 1ch / ~0.6MB.
#     Channels per docs: mono or stereo accepted (stereo uses only the
#     first channel) — mono is strictly safer.
SAMPLE_MAX_DURATION = 20.0   # stop appending after this (docs: 推荐 10~20s)
SAMPLE_MIN_DURATION = 10.0   # pad/loop if below (docs: 推荐 ≥10s)
GAP_DURATION = 1.0           # silence gap between segments (docs: 停顿 ≤2s)


def _filter_candidates(
    all_segments: list[Segment],
    speaker_id: str,
) -> list[Segment]:
    """
    克隆参考候选段的【重叠检测】——这是唯一保留的硬规则。

    diarization 的段里常混着别人插话：候选段只要与任何【其他说话人】的时间
    范围重叠（0.05s 容差，紧邻句不误伤）就淘汰，否则剪进参考素材的音色会被
    第二个人污染。

    情绪/语速之类的文本启发式过滤已按决定移除：参考素材只负责音色，情绪
    交给 TTS 标签，而这类死规则的实际效果是误伤正常句子。
    """
    others = [
        (s.start_time, s.end_time)
        for s in all_segments
        if s.speaker_id != speaker_id
    ]
    eps = 0.05
    kept, dropped = [], []
    for s in sorted(
        (s for s in all_segments if s.speaker_id == speaker_id),
        key=lambda s: s.start_time,
    ):
        if any(o_start < s.end_time - eps and o_end > s.start_time + eps
               for o_start, o_end in others):
            dropped.append(f"{s.id} (overlaps another speaker's span)")
            continue
        kept.append(s)

    if dropped:
        logger.info(
            f"[SegSelect] {speaker_id}: dropped {len(dropped)} candidate(s) "
            f"due to speech overlap: {'; '.join(dropped)[:300]}"
        )
    return kept


def _select_segments_for_speaker(
    segments: list[Segment],
    speaker_id: str,
    candidates: list[Segment] | None = None,
) -> list[Segment]:
    """
    Select segments for a speaker: LONGEST first, stop when accumulated
    duration (voice + gaps) would exceed max.

    Longest-first rather than chronological, deliberately: the docs require
    ≥5s of CONTINUOUS clear speech in the sample, and enrollment does not
    care about timeline order. Chronological greedy could fill the (now
    20s) cap with short utterances and leave the one long segment out —
    a sample with no continuous stretch. Longest-first guarantees the best
    available continuous content is always in.

    `candidates` (when given) is a PRE-FILTERED list from `_filter_candidates`
    (or the omni picker) — the greedy loop then only enforces duration, gaps,
    and mutual overlap.
    """
    if candidates is not None:
        all_speaker_segs = candidates
        speaker_segs = [s for s in candidates if (s.end_time - s.start_time) >= 0.3]
        skipped = len(all_speaker_segs) - len(speaker_segs)
    else:
        all_speaker_segs = [s for s in segments if s.speaker_id == speaker_id]
        # 0.3s floor is not in the docs, but a shorter fragment ("嗯", a cough)
        # cannot be 连续清晰朗读 — it adds exactly the noise the docs exclude.
        speaker_segs = [
            s for s in all_speaker_segs if (s.end_time - s.start_time) >= 0.3
        ]
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

    # Greedily pick LONGEST segments first until max duration
    speaker_segs.sort(key=lambda s: s.end_time - s.start_time, reverse=True)
    selected: list[Segment] = []
    accumulated = 0.0  # voice + gaps total

    for s in speaker_segs:
        dur = s.end_time - s.start_time
        # 同一说话人自己的段也可能互相重叠（diarization 伪影）——最长优先
        # 打乱了时间顺序，重叠检查必须对着【所有】已选段，而不是上一段。
        if any(s.start_time < t.end_time - 0.05 and s.end_time > t.start_time + 0.05
               for t in selected):
            logger.info(f"[SegSelect]   skip {s.id}: overlaps an already selected span")
            continue
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

    # The one docs requirement selection alone cannot always satisfy: if even
    # the longest segment is short of 5s, the sample has no continuous stretch.
    # Nothing to select differently — surface it instead of failing silently.
    longest = max((s.end_time - s.start_time) for s in selected)
    if longest < 5.0:
        logger.warning(
            f"[SegSelect] {speaker_id}: longest segment {longest:.2f}s < 5s — "
            f"the sample has no ≥5s continuous stretch (docs requirement); "
            f"cloning may degrade"
        )

    return selected


def _ffmpeg_build_sample(
    vocals_path: str,
    selected: list[Segment],
    output_path: str,
) -> float:
    """
    Extract segments from vocals, concatenate with 1s silence gaps between them
    (docs: 单次停顿 ≤2s). If total < SAMPLE_MIN_DURATION, loop (repeat)
    segments until minimum is met.
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
    emit: Optional[Callable] = None,
) -> tuple[str, bool]:
    """
    Clone voice for a speaker:
    1. Use LLM to intelligently select the best audio segments
    2. Extract and concatenate sample audio from vocals
    3. Serve it via public URL
    4. Call DashScope voice enrollment
    5. Poll until ready
    6. Return (voice_id, reused)

    The cloned voice_id is cached and persisted to disk. `reused` is False only
    when a NEW enrollment actually happened — the one path that consumes
    enrollment quota; the two cache-hit paths log "ALREADY EXISTS" and are free.

    `emit` (optional) reports the 智能选材 step to the UI: which segments omni
    picked, or that it fell back to the rules. Without it the picker is
    invisible — the user turns the switch on and nothing anywhere says whether
    it ran.
    """
    logger.info(
        f"[Clone] ========== Start voice cloning for {speaker_id} (video={video_id}) =========="
    )
    logger.info(f"[Clone] Total segments in video: {len(segments)}")

    # Check cache first
    if video_id in _cloned_voice_map and speaker_id in _cloned_voice_map[video_id]:
        voice_id = _cloned_voice_map[video_id][speaker_id]
        logger.info(f"[Clone] {speaker_id}: voice ALREADY EXISTS (memory cache): {voice_id}")
        return voice_id, True

    video_dir = get_video_dir(video_id)

    # Check disk cache. The model-mismatch check happens first because it is a
    # cheap local read; the remote liveness probe is only worth paying for a
    # voice that is actually usable with the current model.
    cached_voice_id = _cached_voice_for_model(video_dir, speaker_id)
    if cached_voice_id:
        logger.info(
            f"[Clone] Disk cache hit for {speaker_id}: {cached_voice_id}, verifying..."
        )
        try:
            service = VoiceEnrollmentService()
            info = service.query_voice(voice_id=cached_voice_id)
            status = info.get("status", "UNKNOWN")
            logger.info(f"[Clone] Voice {cached_voice_id} status={status}")
            if status == "OK":
                _cloned_voice_map.setdefault(video_id, {})[speaker_id] = cached_voice_id
                logger.info(
                    f"[Clone] {speaker_id}: voice ALREADY EXISTS on server "
                    f"(disk cache, verified): {cached_voice_id}"
                )
                return cached_voice_id, True
            # Voices idle for over a year are deleted server-side, so a
            # non-OK status is expected occasionally and means re-clone.
            logger.warning(
                f"[Clone] Cached voice {cached_voice_id} status={status}, re-cloning..."
            )
        except Exception as e:
            logger.warning(
                f"[Clone] Failed to verify cached voice {cached_voice_id}: {e}, "
                "re-cloning..."
            )

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

    # Select segments (longest-first, 20s cap, 1s gaps — see the constraints
    # block at the top of this module)
    logger.info(f"[Clone] Step 1a: Selecting segments for {speaker_id}...")
    selected: list[Segment] | None = None
    # 唯一的控制是这个工程的开关（用户点出来的那个），没有全局闸门。
    # 读不到 state 时按关处理：配置未知就不擅自花一次 omni 调用。
    _state = get_state(video_id)
    smart_enabled = bool(_state is not None and _state.clone_smart_pick)
    if smart_enabled and emit is not None:
        await emit({
            "phase": "voice_clone",
            "status": "picking",
            "speaker_id": speaker_id,
        })
    if smart_enabled:
        # 智能选材：omni 听音频挑参考段。任何失败都回退规则 —— 这是增强，
        # 不是依赖。
        try:
            from app.services import omni_service
            candidates = [
                {"id": s.id, "start": s.start_time, "end": s.end_time,
                 "duration": s.end_time - s.start_time, "text": s.text or ""}
                for s in segments
                if s.speaker_id == speaker_id and (s.end_time - s.start_time) >= 0.3
            ]
            audio_url = f"{server_url_base}/api/videos/{video_id}/audio"
            picks, pick_reason = await omni_service.pick_samples(
                video_id, speaker_id, candidates, audio_url
            )
            if picks:
                picked_set = set(picks)
                selected = sorted(
                    (s for s in segments
                     if s.speaker_id == speaker_id and s.id in picked_set),
                    key=lambda s: s.start_time,
                )
                if selected:
                    logger.info(f"[Clone] Step 1a: omni picked {len(selected)} segment(s)")
                    if emit is not None:
                        await emit({
                            "phase": "voice_clone",
                            "status": "picked",
                            "speaker_id": speaker_id,
                            "picked": [s.id for s in selected],
                        })
                else:
                    # picks 非空但一个都对不上本说话人（pick_samples 自带校验，
                    # 这里是兜底）：当作挑选失败回退规则，而不是让后面
                    # "no suitable segments" 硬失败。
                    logger.warning(
                        f"[Clone] Step 1a: omni picked {len(picks)} id(s), none "
                        f"matched {speaker_id} — falling back to rule-based selection"
                    )
                    selected = None
                    if emit is not None:
                        await emit({
                            "phase": "voice_clone",
                            "status": "pick_failed",
                            "speaker_id": speaker_id,
                            "error": "omni 挑出的 id 都不属于该说话人",
                        })
            else:
                logger.warning(
                    f"[Clone] Step 1a: omni picker returned nothing ({pick_reason}) "
                    f"— falling back to rule-based selection"
                )
                if emit is not None:
                    await emit({
                        "phase": "voice_clone",
                        "status": "pick_failed",
                        "speaker_id": speaker_id,
                        # 具体原因（候选不足 / API 失败 / 超轮数）原样带出去：
                        # 界面上这一句是用户唯一能看到的解释。
                        "error": pick_reason or "omni 没有挑出任何段",
                    })
        except Exception as e:
            logger.warning(
                f"[Clone] Step 1a: omni picker failed ({e}) — "
                f"falling back to rule-based selection"
            )
            if emit is not None:
                await emit({
                    "phase": "voice_clone",
                    "status": "pick_failed",
                    "speaker_id": speaker_id,
                    "error": str(e)[:120],
                })
    if selected is None:
        candidates = _filter_candidates(segments, speaker_id)
        selected = _select_segments_for_speaker(segments, speaker_id, candidates=candidates)
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

    # Build sample with ffmpeg (segments + 1s gaps)
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

    # Enroll against the current TTS model: the id returned here can only be
    # synthesized by that exact model.
    target_model = clone_target_model()
    logger.info(
        f"[Clone] Step 3: DashScope voice enrollment — "
        f"model={target_model}, prefix={prefix}, url={sample_url}"
    )

    def _create_voice():
        service = VoiceEnrollmentService()
        voice_id = service.create_voice(
            target_model=target_model,
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

    # Step 5: Cache and persist, tagged with the model it was created for
    logger.info("[Clone] Step 5: Saving voice_id to cache and disk")
    _cloned_voice_map.setdefault(video_id, {})[speaker_id] = voice_id
    save_clone_entry(video_dir, speaker_id, voice_id, target_model)
    logger.info(
        f"[Clone] ========== Voice cloning COMPLETE for {speaker_id}: "
        f"voice_id={voice_id} (target_model={target_model}) =========="
    )

    # A genuinely new enrollment — the one path that consumes quota (0.01 元/音色,
    # 1000 free per account in Beijing). Cache hits above are free.
    return voice_id, False


def forget_cloned_voices(video_id: str) -> None:
    """丢弃这个工程的内存音色映射（删除工程时调用）。

    磁盘上的 voice_clones.json 随目录一起被删，但内存里的 dict 不会 —— 留着
    它，重新上传同一段视频时会直接命中"已克隆"，让一个已经被删掉的工程在内存
    里留下痕迹。
    """
    _cloned_voice_map.pop(video_id, None)


def get_cloned_voice(video_id: str, speaker_id: str) -> Optional[str]:
    """
    Cached cloned voice_id for a speaker, or None.

    Only returns a voice that was enrolled against the *current* TTS model:
    cloned voices cannot cross models, so a stale id would be rejected at
    synthesis time. Deliberately a cheap disk lookup with no network call —
    the pipeline calls this once per segment.
    """
    if video_id in _cloned_voice_map and speaker_id in _cloned_voice_map[video_id]:
        return _cloned_voice_map[video_id][speaker_id]

    voice_id = _cached_voice_for_model(get_video_dir(video_id), speaker_id)
    if voice_id:
        _cloned_voice_map.setdefault(video_id, {})[speaker_id] = voice_id
    return voice_id


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
            model=clone_target_model(),
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
