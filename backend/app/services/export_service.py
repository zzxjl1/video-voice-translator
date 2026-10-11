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
import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from typing import Callable, Optional

from app import config
from app.models import VideoState, get_state, get_video_dir, video_dir_path
from app.services import mix_service, subtitle_service

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
    # No mkdir: `find_export` calls this from read-only endpoints, and creating a
    # directory because someone asked about a project that does not exist is how
    # a GET used to leave empty folders behind.
    return os.path.join(video_dir_path(video_id), name)


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


def resolve_plan(
    state: VideoState, patch: Optional[dict] = None
) -> subtitle_service.ExportPlan:
    """
    The plan an export should use: the saved one, overlaid with `patch`.

    The STYLE always starts from the project's own saved style
    (`state.subtitle_style`, what the style panel edits) and only `patch["style"]`
    keys override it. The plan used to carry a style of its own that nothing ever
    updated, so an embedded/MKV export came out in the config defaults no matter
    what the user had set in the panel — only the browser burn-in honoured it.
    """
    merged = subtitle_service.ExportPlan.from_dict(
        getattr(state, "subtitle_export", None)
    ).to_dict()
    patch = patch or {}
    for key in ("enabled", "format", "tracks", "default_track"):
        if patch.get(key) is not None:
            merged[key] = patch[key]
    project_style = subtitle_service.SubtitleStyle.from_dict(
        getattr(state, "subtitle_style", None)
    ).to_dict()
    merged["style"] = {**project_style, **(patch.get("style") or {})}
    return subtitle_service.ExportPlan.from_dict(merged)


def container_for(plan: subtitle_service.ExportPlan) -> str:
    """Which container a plan needs. Falls back to MP4 for a subtitle-free export."""
    if not plan.enabled or plan.format == "off":
        return "mp4"
    return _CONTAINER_FOR_FORMAT.get(plan.format, "mp4")


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------

def probe_duration(path: str) -> float:
    """时长探测的唯一实现在 subtitle_service.probe_duration（带 mtime 缓存）。"""
    from app.services.subtitle_service import probe_duration as _probe
    return _probe(path) or 0.0


def _decode_to_pcm(path: str, sample_rate: int, atempo: float = 1.0) -> bytes:
    """Decode any audio file to raw mono s16le PCM, optionally time-stretched.

    `atempo` 来自 mix plan，与播放侧的 playbackRate 是**同一条规则**的两种执行
    （同一组夹取界）：预览把这句放到多少倍速，导出就放到多少倍速。
    """
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if atempo and abs(atempo - 1.0) > 1e-4:
        cmd += ["-filter:a", f"atempo={atempo:.4f}"]
    cmd += [
        "-f", "s16le",
        "-acodec", "pcm_s16le",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed to decode {path}: "
            f"{result.stderr.decode('utf-8', errors='replace')[-400:]}"
        )
    return result.stdout


# ---------------------------------------------------------------------------
# Timeline assembly
# ---------------------------------------------------------------------------

def build_dubbed_timeline(
    video_id: str,
    state: VideoState,
    duration: float,
    plan: dict,
    progress: Optional[Callable[[int, int], None]] = None,
) -> Optional[str]:
    """
    按 mix plan 把每段配音写进一条 WAV。返回 WAV 路径，一段都放不进去时 None。

    每段怎么摆全部来自 plan（mix_service 生成，与播放同源）：atempo 先把这句
    变速到与预览一致，`trim` 只在"夹到上限仍装不下"时出现，首尾淡出消掉 TTS
    边缘的咔哒声。以前这里是无脑按原始速度摆位相加 —— 超槽的句子会和下一句
    叠着响，而且与预览的速率规则完全是两套。

    每次重建。缓存它意味着要记录每个 clip 的身份才知道何时失效 —— 记账成本
    高于它省下的几秒钟，而且这是用户主动触发的操作，不在循环里。
    """
    import numpy as np

    sample_rate = config.EXPORT_SAMPLE_RATE
    path_by_id = {seg.id: seg.audio_path for seg in state.segments}
    entries = plan["segments"]
    if not entries:
        logger.warning(f"[{video_id}] No synthesized segments available for export")
        return None

    total_samples = int(duration * sample_rate) + sample_rate  # +1s tail
    if total_samples <= 0:
        raise RuntimeError("Cannot determine video duration for export")

    timeline_path = os.path.join(get_video_dir(video_id), "dubbed_timeline.wav")
    raw_path = f"{timeline_path}.raw"
    # 时间轴用**磁盘映射**（memmap）而不是内存数组（P2 #26）：2 小时 @24kHz 的
    # int32 缓冲是 ~691MB —— RSS 不与视频时长成正比。
    timeline = np.memmap(raw_path, dtype=np.int32, mode="w+", shape=(total_samples,))

    fade_samples = max(0, int(plan.get("voiceFadeS", 0.02) * sample_rate))
    placed = 0
    for index, entry in enumerate(entries):
        audio_path = path_by_id.get(entry["id"])
        if not audio_path or not os.path.exists(audio_path):
            logger.warning(
                f"[{video_id}] plan segment {entry['id']}: audio file missing, skipped"
            )
            continue
        try:
            pcm = _decode_to_pcm(audio_path, sample_rate, entry.get("atempo") or 1.0)
        except Exception as e:
            logger.warning(f"[{video_id}] Skipping segment {entry['id']}: {e}")
            continue

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.int32)
        slot_samples = int(max(0.0, entry["end"] - entry["start"]) * sample_rate)
        cap = int(entry["trim"] * sample_rate) if entry.get("trim") else slot_samples
        samples = samples[: max(0, min(len(samples), cap))]
        if not len(samples):
            continue

        # 首尾淡出。这里不做响度处理 —— 响度归一在 mux 的母带步骤里对整条混音
        # 做一次（逐段拉平会毁掉轻重与情绪）。
        fade = min(fade_samples, len(samples) // 4)
        if fade > 0:
            ramp = np.linspace(0.0, 1.0, fade)
            samples[:fade] = (samples[:fade] * ramp).astype(np.int32)
            samples[-fade:] = (samples[-fade:] * ramp[::-1]).astype(np.int32)

        start_sample = int(entry["start"] * sample_rate)
        end = min(total_samples, start_sample + len(samples))
        if end > start_sample:
            timeline[start_sample:end] += samples[: end - start_sample]
        placed += 1

        if progress and (index % 5 == 0 or index == len(entries) - 1):
            try:
                progress(index + 1, len(entries))
            except Exception as e:
                # 回调是 UI 的进度条。它抛错不该中断导出，但也不能静默 ——
                # 静默的话现象是"进度条永远卡在同一格"，而服务器日志里什么都
                # 没有，排查时完全无从下手。
                logger.warning(f"[{video_id}] export progress callback failed: {e}")

    if placed == 0:
        del timeline
        try:
            os.remove(raw_path)
        except OSError:
            pass
        return None

    timeline_path = os.path.join(get_video_dir(video_id), "dubbed_timeline.wav")
    with wave.open(timeline_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(sample_rate)
        # 分块 裁剪→int16→写出：不构造整条时间轴的字节串。
        chunk_samples = 1 << 20          # 约 4MB int32 / 2MB int16 每块
        for start in range(0, int(timeline.shape[0]), chunk_samples):
            block = timeline[start : start + chunk_samples]
            wav.writeframes(
                np.clip(block, MIN_SAMPLE, MAX_SAMPLE).astype(np.int16).tobytes()
            )
    del timeline                          # 先解除映射，再删中间文件
    try:
        os.remove(raw_path)
    except OSError:
        # 残留一个 .raw 不影响导出结果（下次导出会覆盖同名文件）。
        logger.warning(f"[{video_id}] could not remove {raw_path}")

    logger.info(
        f"[{video_id}] Dubbed timeline built: {placed}/{len(entries)} segments, "
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
# Bed (垫底轨) & loudness
# ---------------------------------------------------------------------------

def build_ducked_bed(
    video_id: str,
    state: VideoState,
    plan: dict,
    duration: float,
) -> Optional[str]:
    """
    按 plan 生成垫底轨：解码一次，逐样本乘上包络与基准增益，写成 mono WAV。

    为什么在 python 里做而不用 ffmpeg 的 sidechaincompress：包络必须与播放侧
    求值的是**同一条**（plan.bed.duck 的分段线性断点）；sidechaincompress 是
    另一套压缩器模型，深度与快慢都无法和预览逐点一致 —— 那正是这次要消灭的
    "两套实现"。

    分块处理：解码与写出都是流式的，内存与视频时长无关（2 小时也是常数）。
    垫底比视频长时在读满时长后停住 —— 以前 amix duration=longest 会让成片
    音轨长出尾巴。
    """
    import numpy as np

    bed = plan.get("bed")
    if not bed:
        return None

    sample_rate = config.EXPORT_SAMPLE_RATE
    if bed["kind"] == "background":
        source = os.path.join(get_video_dir(video_id), "background.wav")
    else:
        source = state.file_path
    if not source or not os.path.exists(source):
        raise RuntimeError(f"垫底音源不存在：{source}")

    duck = bed.get("duck") or []
    xs = [p[0] for p in duck]
    ys = [p[1] for p in duck]
    base_gain = float(bed["gain"])

    out_path = os.path.join(get_video_dir(video_id), "bed_ducked.wav")
    max_samples = int((duration + 0.5) * sample_rate)
    chunk_bytes = sample_rate * SAMPLE_WIDTH          # 每块 1 秒

    cmd = ["ffmpeg", "-v", "error", "-i", source]
    if bed["kind"] == "original":
        cmd += ["-vn"]                                # 只要音轨
    cmd += ["-f", "s16le", "-ac", "1", "-ar", str(sample_rate), "-"]

    with tempfile.TemporaryFile() as err_file:
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file)
        peak = 0.0
        written = 0
        try:
            with wave.open(out_path, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(SAMPLE_WIDTH)
                wav.setframerate(sample_rate)
                while written < max_samples:
                    raw = process.stdout.read(chunk_bytes) if process.stdout else b""
                    if not raw:
                        break
                    block = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
                    times = (written + np.arange(len(block))) / sample_rate
                    # np.interp = 分段线性 —— 与前端对 plan 断点的求值方式一致
                    gain = base_gain * (
                        np.interp(times, xs, ys) if xs else 1.0
                    )
                    block *= gain
                    if len(block):
                        peak = max(peak, float(np.max(np.abs(block))))
                    wav.writeframes(
                        np.clip(block, MIN_SAMPLE, MAX_SAMPLE).astype(np.int16).tobytes()
                    )
                    written += len(block)
            code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
            err_file.seek(0)
            stderr = err_file.read().decode("utf-8", errors="replace")

    if code != 0:
        raise RuntimeError(f"ffmpeg bed decode failed: {stderr[-400:]}")
    if written == 0:
        raise RuntimeError(f"垫底音源解不出任何音频：{source}")
    logger.info(
        f"[{video_id}] Ducked bed built ({bed['kind']}, gain {bed['gain']:.3f}, "
        f"{'%d duck pts' % len(duck) if duck else 'no ducking'}, "
        f"peak {20 * __import__('math').log10(max(peak, 1e-9)):.1f} dBFS) -> {out_path}"
    )
    return out_path


_EBUR128_NUMBER = r"(-?(?:inf|\d+(?:\.\d+)?))"


def parse_ebur128_summary(stderr: str) -> tuple[Optional[float], float]:
    """
    ffmpeg `ebur128=peak=sample` 的 Summary 段 → (整合响度 LUFS, 采样峰值 dBFS)。

    纯函数，单独拿出来是为了能用一段真实输出直接单测。响度读成 -70.0（EBU R128
    的绝对门限）或 -inf 表示纯静音，返回 None；峰值 -inf 原样返回。缺字段就抛 ——
    一次读不懂的测量不能悄悄变成"增益 0"。
    """
    summary = stderr[stderr.rfind("Summary:"):] if "Summary:" in stderr else ""
    loudness = re.search(r"I:\s+" + _EBUR128_NUMBER + r"\s+LUFS", summary)
    peak = re.search(r"Sample peak:\s+Peak:\s+" + _EBUR128_NUMBER + r"\s+dBFS", summary)
    if not loudness or not peak:
        raise RuntimeError(f"响度测量没有产出可读的结果: {stderr[-400:]}")
    lufs = float(loudness.group(1))
    return (None if lufs <= mix_service.SILENCE_LUFS else lufs), float(peak.group(1))


def _measure_mix(timeline_path: str, bed_path: Optional[str]) -> tuple[Optional[float], float]:
    """
    量整条混音的 (整合响度, 采样峰值)。失败就抛，绝不猜。

    只读配音时间轴与垫底这两个小 WAV，**不碰视频**：以前量响度是把整个原视频
    也作为输入打开的（只是不映射，但解复用仍要把它从头读到尾）。BS.1770 本身
    比 loudnorm 第一遍便宜一个数量级（37 分钟混音实测 1.0 秒 vs 25.5 秒）。
    """
    if bed_path:
        inputs = ["-i", timeline_path, "-i", bed_path]
        pre = "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[mix];[mix]"
    else:
        inputs = ["-i", timeline_path]
        pre = "[0:a]"
    # -v info：Summary 走的是 info 级日志，warning 会把它吞掉。
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats", "-v", "info", *inputs,
        "-filter_complex", f"{pre}ebur128=peak=sample:framelog=quiet[o]",
        "-map", "[o]", "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"响度测量失败: {result.stderr[-400:] if result.stderr else 'unknown'}"
        )
    return parse_ebur128_summary(result.stderr or "")


def master_filter(video_id: str, lufs: Optional[float], peak_dbfs: float, master: dict) -> str:
    """
    母带滤镜串：一次静态增益 → 升到 48 kHz → 限幅器兜底。

    增益的决定在 `mix_service.master_gain`（策略归那边）；这里只负责把它翻成滤镜
    并把结果说清楚。顺序是有讲究的：限幅器放在升采样**之后**。先限 24 kHz 再升
    采样，过采样产生的采样间峰值会冲出上限（真工程实测 -1.30 dBTP，上限 -1.5）；
    升采样后再限，成片真峰正好落在 -1.50。
    """
    decision = mix_service.master_gain(lufs, peak_dbfs, master)
    gain = decision["gainDb"]

    if decision["limitedBy"] == "unmeasurable":
        logger.warning(
            f"[{video_id}] 混音响度测不出（静音？）：不做增益，只留限幅 "
            f"(peak {peak_dbfs:.1f} dBFS)"
        )
    else:
        shortfall = decision["wantedDb"] - gain
        message = (
            f"[{video_id}] Master: mix {lufs:.1f} LUFS / peak {peak_dbfs:.1f} dBFS "
            f"-> gain {gain:+.2f} dB (target {master['targetLufs']} LUFS), "
            f"limiter reduces up to {decision['expectedLimitDb']:.1f} dB at the peaks"
        )
        if shortfall > 0.5:
            reason = {
                "peak": f"峰值太尖，限幅超过 {master['maxLimitingDb']} dB 会压扁波形",
                "boost": f"单次抬升上限 {master['maxBoostDb']} dB",
            }[decision["limitedBy"]]
            logger.warning(f"{message}；比目标低 {shortfall:.1f} dB（{reason}）")
        else:
            logger.info(message)

    # alimiter.limit 是线性值，范围 [0.0625, 1]（-24 dB .. 0 dB）。
    ceiling_db = min(0.0, max(-24.0, float(master["peakLimitDbfs"])))
    limit = 10.0 ** (ceiling_db / 20.0)
    return (
        f"volume={gain:.3f}dB,aresample=48000,"
        f"alimiter=limit={limit:.6f}:attack=5:release=50:level=disabled:latency=1"
    )


def _mux(
    video_id: str,
    video_path: str,
    timeline_path: str,
    bed_path: Optional[str],
    output_path: str,
    duration: float,
    subtitle_inputs: Optional[list[SubtitleInput]] = None,
    default_subtitle: int = 0,
    master: Optional[dict] = None,
) -> None:
    """
    Combine the original video stream + dubbed timeline (+ ducked bed + subtitles).

    音频链：amix（normalize=0；duration=first —— 以前对背景用 longest，垫底比
    视频长时成片音轨会长出尾巴）→ 母带：量一次响度、整条加同一个静态增益、
    限幅器兜住峰值（见 `master_filter`）。

    The video stream is always copied. Subtitles are EMBEDDED, never burned:
    embedding is a remux (~40 ms), burning would need a full re-encode plus
    libass and a CJK font, none of which this host has. Burn-in runs in the
    browser instead.
    """
    master = master or {}
    inputs = subtitle_inputs or []

    base_cmd = ["ffmpeg", "-y", "-v", "warning", "-i", video_path, "-i", timeline_path]
    if bed_path:
        base_cmd += ["-i", bed_path]
    # 字幕输入最后加：filter_complex 按序号引用 0/1/2，提前插入会悄悄把音频
    # 映射到错的流上。
    first_subtitle_input = 3 if bed_path else 2
    for subtitle_input in inputs:
        base_cmd += ["-i", subtitle_input.path]

    if bed_path:
        pre_filter = "[1:a][2:a]amix=inputs=2:duration=first:normalize=0[mix];[mix]"
    else:
        pre_filter = "[1:a]"

    lufs, peak_dbfs = _measure_mix(timeline_path, bed_path)
    filter_complex = f"{pre_filter}{master_filter(video_id, lufs, peak_dbfs, master)}[aout]"

    cmd = base_cmd + [
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
        # 滤镜链末端已经是 48 kHz；显式写出来，是为了不依赖链里谁改了采样率。
        "-ar", "48000",
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

    # Written beside the target and renamed into place only on success. Muxing
    # straight onto `output_path` meant a failed (or killed) export destroyed the
    # previous good file, which `export_video` explicitly promises not to do.
    base, extension = os.path.splitext(output_path)
    partial_path = f"{base}.part{extension}"
    cmd.append(partial_path)

    logger.info(f"[{video_id}] Muxing: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"ffmpeg mux failed: {result.stderr[-800:] if result.stderr else 'unknown error'}"
            )
        os.replace(partial_path, output_path)
    finally:
        if os.path.exists(partial_path):
            try:
                os.remove(partial_path)
            except OSError:
                pass


# One export per video at a time: they share `dubbed_timeline.wav`, `bed_ducked.wav`
# and the output name, so two concurrent runs would interleave writes into the same
# files and could ship a corrupt mix.
_export_locks: dict[str, asyncio.Lock] = {}


async def export_video(
    video_id: str,
    emit: Optional[Callable] = None,
    plan: Optional[subtitle_service.ExportPlan] = None,
) -> ExportResult:
    """
    Produce the final dubbed video for a video.

    `plan` is the subtitle plan for THIS export. When omitted it is resolved from
    the video's saved state (`resolve_plan`). Passing it explicitly is what lets
    an export use a plan that must not be persisted (the browser burn-in asks for
    a subtitle-free file without changing the project's saved preference).

    Emits progress events through `emit` when provided (async callable).
    """
    lock = _export_locks.setdefault(video_id, asyncio.Lock())
    async with lock:
        return await _export_video_locked(video_id, emit, plan)


async def _export_video_locked(
    video_id: str,
    emit: Optional[Callable],
    plan: Optional[subtitle_service.ExportPlan],
) -> ExportResult:
    if not is_available():
        raise RuntimeError("ffmpeg/ffprobe not found — export requires them on PATH")

    state = get_state(video_id)
    if not state:
        raise RuntimeError("Video not found")

    plan = plan or resolve_plan(state)
    container = container_for(plan)

    async def _emit(event: dict) -> None:
        if emit:
            await emit(event)

    await _emit({"phase": "export", "status": "started"})

    duration = await asyncio.get_running_loop().run_in_executor(
        None, probe_duration, state.file_path
    )
    if duration <= 0:
        raise RuntimeError("Failed to probe the video duration")

    # 混音计划：mix_service 从 state 算出的唯一权威（数值/包络/摆位）。
    # 执行端**现场**生成 —— 永远新鲜，不存在"用过期 plan"的问题；播放端拿到的
    # 是同一份生成器的输出（见 /status 的 mix 字段）。
    mix_plan = await asyncio.get_running_loop().run_in_executor(
        None, lambda: mix_service.build_mix_plan(state)
    )

    loop = asyncio.get_running_loop()
    progress_queue: asyncio.Queue = asyncio.Queue()

    def on_progress(current: int, total: int) -> None:
        loop.call_soon_threadsafe(progress_queue.put_nowait, (current, total))

    timeline_task = asyncio.ensure_future(
        loop.run_in_executor(
            None,
            lambda: build_dubbed_timeline(
                video_id, state, duration, mix_plan, progress=on_progress
            ),
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

    await _emit({"phase": "export", "status": "bed"})

    bed_path = await asyncio.get_running_loop().run_in_executor(
        None, lambda: build_ducked_bed(video_id, state, mix_plan, duration)
    )

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
            bed_path,
            output_path,
            duration,
            subtitle_inputs,
            default_index,
            master=mix_plan["master"],
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
