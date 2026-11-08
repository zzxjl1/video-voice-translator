"""
声音克隆：给一位说话人造一个音色（DashScope voice enrollment）。

本模块只做"挑素材 → 拼样本 → 注册 → 等就绪"，**不碰任何项目状态**：
  输入  说话人的行 + （可选）指定的样本行
  输出  voice_id
登记到哪、是否复用，都是调用方（命令层）的事，音色存在 `Speaker.clone`
（`{"voice_id", "target_model", "created_at"}`）里。旧版有三份缓存（进程内 dict、
voice_clone_map.json、每段合成时再读一次）—— 现在只有项目文档里这一份。

音色绑定模型
------------
官方文档：「音色在创建时通过 target_model 绑定到特定的语音合成模型，不能跨模型
使用」。拿旧模型的 voice_id 去合成会报 InvalidParameter / Engine error [411]，所以
`Speaker.clone` 带着 `target_model`，`status.clone_voice_id` 只在它等于当前
`config.TTS_MODEL` 时才认。
"""
import asyncio
import logging
import os
import subprocess
from typing import Callable, Optional

import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService

from app import config
from app.models import Segment, get_video_dir, video_dir_path
from app.services import usage_service

logger = logging.getLogger(__name__)

dashscope.api_key = config.DASHSCOPE_API_KEY


def clone_target_model() -> str:
    """克隆音色注册到哪个模型：始终跟随 TTS 模型，两者不可能漂移（必须完全一致）。"""
    return config.TTS_MODEL


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
    # selected 可能为空（规则选材一条都没选到）：max() 对空序列会抛 ValueError，
    # 把本意的"警告一声继续"变成整段崩溃。
    longest = max((s.end_time - s.start_time for s in selected), default=0.0)
    if not selected:
        logger.warning(
            f"[SegSelect] {speaker_id}: no usable segment at all — "
            f"cloning will be skipped for this speaker"
        )
    elif longest < 5.0:
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

    # timeout 是必需的：ffmpeg 卡在损坏输入/坏挂载上会永久占住一个线程池 worker，
    # 而整条管线在等它。超时会让 subprocess 杀掉子进程并抛 TimeoutExpired。
    result = subprocess.run(
        cmd, check=True, capture_output=True, timeout=config.CLONE_SAMPLE_TIMEOUT
    )
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


def candidate_lines(segments: list[Segment], speaker_id: str) -> list[Segment]:
    """该说话人可作参考的行：语音、够长、不与别人重叠。"""
    pool = [s for s in segments if s.kind == "speech" and (s.end_time - s.start_time) >= 0.3]
    return _filter_candidates(pool, speaker_id)


def validate_chosen(
    segments: list[Segment], speaker_id: str, chosen_ids: list[str]
) -> list[Segment]:
    """
    校验 AI / 用户指定的样本行：必须存在、属于这个说话人、是语音、彼此不重叠、
    总时长在 [1s, 60s]（官方硬上限 60s）。通过则按时间排序返回，否则抛带人话的错。
    """
    by_id = {s.id: s for s in segments}
    unknown = [i for i in chosen_ids if i not in by_id]
    if unknown:
        raise ValueError(f"找不到这些行：{unknown}")
    picked = [by_id[i] for i in dict.fromkeys(chosen_ids)]
    foreign = [s.id for s in picked if s.speaker_id != speaker_id]
    if foreign:
        raise ValueError(f"这些行不属于说话人 {speaker_id}：{foreign}")
    not_speech = [s.id for s in picked if s.kind != "speech"]
    if not_speech:
        raise ValueError(f"这些行不是语音（音乐/噪音）：{not_speech}")
    picked.sort(key=lambda s: s.start_time)
    for a, b in zip(picked, picked[1:]):
        if a.end_time - b.start_time > 0.05:
            raise ValueError(f"样本行互相重叠：{a.id} 与 {b.id}")
    total = sum(s.end_time - s.start_time for s in picked)
    if total < 1.0:
        raise ValueError(f"样本总共只有 {total:.1f} 秒（至少 1 秒，建议 10~20 秒）")
    if total > 60.0:
        raise ValueError(f"样本总共 {total:.1f} 秒，超过官方上限 60 秒")
    return picked


async def pick_reference_lines(
    video_id: str,
    segments: list[Segment],
    speaker_id: str,
    audio_url: str,
    smart: bool,
    emit: Optional[Callable] = None,
) -> list[Segment]:
    """
    选参考行：`smart` 时先让 omni 听着挑，任何失败都回退规则（最长优先 + 去重叠）。
    返回至少一行，否则抛 RuntimeError。
    """
    async def say(status: str, **kw):
        if emit is not None:
            await emit({"phase": "voice_clone", "status": status, "speaker_id": speaker_id, **kw})

    pool = candidate_lines(segments, speaker_id)
    if smart:
        await say("picking")
        try:
            from app.services import omni_service
            picks, reason = await omni_service.pick_samples(
                video_id, speaker_id,
                [{"id": s.id, "start": s.start_time, "end": s.end_time,
                  "duration": s.end_time - s.start_time, "text": s.text or ""} for s in pool],
                audio_url,
            )
            chosen = [s for s in pool if picks and s.id in set(picks)]
            if chosen:
                await say("picked", picked=[s.id for s in chosen])
                return sorted(chosen, key=lambda s: s.start_time)
            await say("pick_failed", error=reason or "omni 挑出的行都不在候选里")
        except Exception as e:  # 增强不是依赖：失败回退规则
            logger.warning(f"[Clone] omni picker failed ({e}) — using the rules")
            await say("pick_failed", error=str(e)[:120])

    selected = _select_segments_for_speaker(segments, speaker_id, candidates=pool)
    if not selected:
        raise RuntimeError(f"说话人 {speaker_id} 没有可用于克隆的素材")
    return selected


def sample_source(video_id: str) -> str:
    """参考音频来源：优先全频带人声（vocals.wav），否则抽出的整条音频。"""
    base = video_dir_path(video_id)
    for name in ("vocals.wav", "extracted_audio.wav"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    raise RuntimeError("没有可用于克隆的音频文件")


async def enroll_voice(
    video_id: str,
    speaker_id: str,
    selected: list[Segment],
    server_url_base: str,
    emit: Optional[Callable] = None,
) -> str:
    """
    拼样本 → 注册音色 → 轮询到就绪，返回 voice_id。每一步都有各自的超时上限
    （一个挂住的请求不能永久占住线程池里的 worker）。

    新建音色是计费/配额动作（0.01 元/个，账号 1000 个免费额度，删除不返还），所以
    记账点只放在"创建成功"这一行。
    """
    async def say(status: str, **kw):
        if emit is not None:
            await emit({"phase": "voice_clone", "status": status, "speaker_id": speaker_id, **kw})

    loop = asyncio.get_running_loop()
    total_voice = sum(s.end_time - s.start_time for s in selected)
    if total_voice < 1.0:
        raise RuntimeError(f"说话人 {speaker_id} 的素材只有 {total_voice:.1f}s（至少 1s）")

    # —— 拼样本 ——
    speaker_dir = os.path.join(get_video_dir(video_id), "voice_clone", speaker_id)
    os.makedirs(speaker_dir, exist_ok=True)
    sample_path = os.path.join(speaker_dir, "sample.wav")
    await say("sampling", seconds=round(total_voice, 1))
    try:
        await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: _ffmpeg_build_sample(sample_source(video_id), selected, sample_path)
            ),
            timeout=config.CLONE_SAMPLE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"参考音频拼接超时（>{config.CLONE_SAMPLE_TIMEOUT}s）") from None

    # —— 注册 ——
    sample_url = f"{server_url_base}/api/projects/{video_id}/voice-sample/{speaker_id}"
    prefix = "".join(
        c for c in f"v{video_id[:8]}{speaker_id.replace(' ', '').lower()[:6]}" if c.isalnum()
    )[:10]
    target_model = clone_target_model()
    logger.info(f"[Clone] enrolling {speaker_id}: model={target_model} prefix={prefix} url={sample_url}")

    def _create() -> str:
        return VoiceEnrollmentService().create_voice(
            target_model=target_model, prefix=prefix, url=sample_url
        )

    voice_id: Optional[str] = None
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            if attempt == 1:
                await say("enrolling", timeout_s=config.CLONE_ENROLL_TIMEOUT)
            try:
                voice_id = await asyncio.wait_for(
                    loop.run_in_executor(None, _create), timeout=config.CLONE_ENROLL_TIMEOUT
                )
            except asyncio.TimeoutError:
                # asyncio 的超时是"放弃等待"而不是"杀掉那个调用"：线程里的请求会自己
                # 跑完，结果丢弃。消息要说人话，否则日志里只剩一个空白的 TimeoutError。
                raise RuntimeError(
                    f"注册音色超时（>{config.CLONE_ENROLL_TIMEOUT}s，DashScope 无响应或正在排队）"
                ) from None
            usage_service.record(
                video_id=video_id, step="clone", model="qwen-voice-enrollment",
                detail=f"{speaker_id} -> {target_model}", usage={"voices": 1},
            )
            break
        except Exception as e:
            msg = str(e)
            transient = any(k in msg for k in ("InputDownloadFailed", "download audio failed", "HTTP 415"))
            logger.warning(f"[Clone] create_voice attempt {attempt}/{attempts} failed: {msg}")
            if attempt == attempts or not transient:
                raise
            await asyncio.sleep(3)
    if voice_id is None:
        raise RuntimeError(f"为 {speaker_id} 创建音色失败（已重试）")

    # —— 轮询 ——
    # 上限落在**单次调用**上，不是整个窗口：曾经把单次上限套在整个循环外面，等于把
    # 30×10s 压成 30s，enrollment 稍慢的说话人就整段判死。
    service = VoiceEnrollmentService()
    last = "UNKNOWN"
    for i in range(1, config.CLONE_POLL_MAX_ATTEMPTS + 1):
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: service.query_voice(voice_id=voice_id)),
                timeout=config.CLONE_POLL_CALL_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001 - 单次挂住/抖动：记一行继续等（TimeoutError 也在内）
            logger.warning(f"[Clone] poll {i}/{config.CLONE_POLL_MAX_ATTEMPTS}: {e or 'timeout'}")
            await asyncio.sleep(config.CLONE_POLL_INTERVAL_S)
            continue
        last = (info or {}).get("status", "UNKNOWN")
        if last == "OK":
            return voice_id
        if last == "UNDEPLOYED":
            raise RuntimeError(f"为 {speaker_id} 克隆失败（UNDEPLOYED），素材质量可能不足")
        await asyncio.sleep(config.CLONE_POLL_INTERVAL_S)
    raise RuntimeError(
        f"为 {speaker_id} 克隆超时：{config.CLONE_POLL_MAX_ATTEMPTS} 次轮询后仍是 {last}"
    )


async def verify_voice(voice_id: str) -> bool:
    """已登记的音色在服务端是否还活着（闲置超过一年会被删）。网络问题按"不确定"返回 False。"""
    try:
        info = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: VoiceEnrollmentService().query_voice(voice_id=voice_id)
            ),
            timeout=config.CLONE_POLL_CALL_TIMEOUT,
        )
        return (info or {}).get("status") == "OK"
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Clone] could not verify {voice_id}: {e}")
        return False


_PREVIEW_TEXTS = {
    "Chinese": "你好，这是克隆后的声音效果展示。",
    "English": "Hello, this is a demonstration of the cloned voice.",
    "Japanese": "こんにちは、これはクローンされた音声のデモです。",
    "Korean": "안녕하세요, 이것은 복제된 음성의 데모입니다.",
    "French": "Bonjour, ceci est une démonstration de la voix clonée.",
    "German": "Hallo, dies ist eine Demonstration der geklonten Stimme.",
    "Spanish": "Hola, esta es una demostración de la voz clonada.",
}


def preview_path(video_id: str, label: str) -> str:
    """只读：被 GET 路由调用，所以不能建目录（`get_video_dir` 会 makedirs）。写入处自己建。"""
    return os.path.join(video_dir_path(video_id), "voice_clone", label, "preview.mp3")


async def preview_voice(video_id: str, voice: str, model: str, language: str, label: str) -> str:
    """用某个音色（克隆或系统）合成一句固定的试听语，返回 mp3 路径。"""
    from app.services import tts_service
    text = _PREVIEW_TEXTS.get(language, _PREVIEW_TEXTS["English"])
    path = preview_path(video_id, label)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def _go() -> str:
        audio = tts_service._synth_audio(model, voice, text, video_id=video_id, detail=f"preview({label})")
        if not audio:
            raise RuntimeError("试听合成返回了空音频")
        with open(path, "wb") as f:
            f.write(audio)
        return path

    return await asyncio.get_running_loop().run_in_executor(None, _go)
