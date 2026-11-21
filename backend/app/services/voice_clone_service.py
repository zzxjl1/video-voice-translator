"""
声音克隆：给一位说话人造一个音色（DashScope voice enrollment）。

本模块只做"挑素材（见 voice_presets）→ 拼样本 → 注册 → 等就绪"，**不碰任何项目状态**：
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
import base64
import logging
import os
import subprocess
from typing import Callable, Optional

import dashscope
from dashscope.audio.tts_v2 import VoiceEnrollmentService

from app import config
from app.models import Segment, get_video_dir, video_dir_path
from app.services import usage_service, voice_presets as presets

logger = logging.getLogger(__name__)

dashscope.api_key = config.DASHSCOPE_API_KEY


def clone_target_model() -> str:
    """克隆音色注册到哪个模型：始终跟随 TTS 模型，两者不可能漂移（必须完全一致）。"""
    return config.TTS_MODEL


# 样本约束，对齐官方「声音复刻」文档（Qwen-Audio-TTS）：
#   - 时长推荐 10~20 秒，硬上限 60 秒 → 上限取推荐的 20，超出推荐窗口没有收益。
#   - 单次停顿 ≤ 2 秒 → 拼接间隔 0.5 秒，远在范围内。
#   - 必须包含至少 5 秒连续清晰的朗读内容。
#   - 格式 WAV/MP3/M4A、≤10MB、采样率 ≥16kHz → 输出统一 24kHz 单声道 pcm_s16le
#     （20 秒约 1MB），满足全部要求。
#   - 服务端预处理默认只截**前 10 秒**（`max_prompt_audio_length` 默认 10）→ 注册时
#     显式传 SAMPLE_MAX_DURATION，否则 13~20 秒的样本后半段白做。实测同一份样本放宽
#     到 20 秒后音色明显更好。
SAMPLE_MAX_DURATION = presets.SAMPLE_MAX_DURATION
SAMPLE_MIN_DURATION = 10.0   # 只用于提示"偏短"，**不再循环重复凑时长**


def _ffmpeg_build_sample(vocals_path: str, plan: presets.Plan, output_path: str) -> float:
    """
    按计划截取并拼接，输出 24kHz 单声道 wav，返回总时长。

    和旧版的区别（都是为了减少"口音 / 韵律泄漏"和拼接痕迹）：
      · 每段首尾 20ms 淡入淡出：硬切会在拼接处留下咔哒声，模型可能把它当"停顿特征"学进去。
      · 间隔用极低电平的粉红噪声而不是数字静音：和前后真实录音的底噪连续。
      · 不循环：素材不够就用现有的，宁可偏短也不重复同几句（重复只会放大它们的韵律）。
      · 多段拼接时不再整体 loudnorm（实测更自然）；单段保留，让响度落在 DashScope 友好范围。
    """
    clips = plan.clips
    if not clips:
        raise RuntimeError("没有可截取的素材")
    fade = presets.FADE_DURATION
    parts: list[str] = []
    labels: list[str] = []
    for i, (a, b) in enumerate(clips):
        dur = b - a
        f = min(fade, dur / 4)
        parts.append(
            f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates=24000:channel_layouts=mono,"
            f"afade=t=in:d={f:.3f},afade=t=out:st={max(0.0, dur - f):.3f}:d={f:.3f}[a{i}]"
        )
        labels.append(f"[a{i}]")
        if i < len(clips) - 1 and plan.gap > 0:
            parts.append(
                f"anoisesrc=d={plan.gap}:c=pink:a=0.0005:r=24000,"
                f"aformat=sample_fmts=fltp:channel_layouts=mono[g{i}]"
            )
            labels.append(f"[g{i}]")
    tail = "loudnorm=I=-16:TP=-1.5:LRA=11" if len(clips) == 1 else "anull"
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[raw]")
    parts.append(f"[raw]{tail}[out]")
    filter_complex = ";".join(parts)

    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", vocals_path,
        "-filter_complex", filter_complex, "-map", "[out]",
        "-ar", "24000", "-ac", "1", "-acodec", "pcm_s16le", "-sample_fmt", "s16",
        output_path,
    ]
    logger.info(f"[Clone] sample[{plan.preset}]: {len(clips)} clip(s), ≈{plan.seconds:.1f}s, lines={plan.line_ids}")
    # timeout 是必需的：ffmpeg 卡在损坏输入 / 坏挂载上会永久占住一个线程池 worker。
    subprocess.run(cmd, check=True, capture_output=True, timeout=config.CLONE_SAMPLE_TIMEOUT)
    if not os.path.exists(output_path):
        raise RuntimeError(f"参考样本没有生成：{output_path}")
    if plan.seconds < SAMPLE_MIN_DURATION:
        logger.warning(
            f"[Clone] 样本只有 {plan.seconds:.1f}s（建议 ≥{SAMPLE_MIN_DURATION:.0f}s）——"
            f"素材有限，音色可能不稳；不会循环重复来凑时长"
        )
    longest = max(b - a for a, b in clips)
    if longest < 5.0:
        logger.warning(f"[Clone] 样本里最长一段 {longest:.1f}s < 5s，没有连续朗读段（文档要求），克隆可能变差")
    return plan.seconds


def candidate_lines(segments: list[Segment], speaker_id: str) -> list[Segment]:
    """该说话人可作参考的行：语音、够长、不与别人重叠。"""
    return presets.pool_for(segments, speaker_id)


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


async def pick_plan(
    video_id: str,
    segments: list[Segment],
    speaker_id: str,
    preset: Optional[str] = None,
    *,
    audio_url: str = "",
    target_language: str = "",
    smart: bool = False,
    emit: Optional[Callable] = None,
) -> presets.Plan:
    """
    选参考素材，返回计划。

    · 给了 `preset`：规则型预设直接算；`smart` 让 omni 听着挑（失败抛错，因为用户明确点了它）。
    · 没给（初始化 / 默认克隆）：`smart` 开着先让 omni 挑，任何失败都退到按素材量自适应的
      默认预设（中句拼接 → 连读 → 单句，不硬凑）。
    素材一句都没有 → 抛 RuntimeError。
    """
    async def say(status: str, **kw):
        if emit is not None:
            await emit({"phase": "voice_clone", "status": status, "speaker_id": speaker_id, **kw})

    async def ask_omni() -> presets.Plan:
        from app.services import omni_service
        pool = presets.pool_for(segments, speaker_id)
        await say("picking")
        picks, reason = await omni_service.pick_samples(
            video_id, speaker_id,
            [{"id": s.id, "start": s.start_time, "end": s.end_time,
              "duration": s.end_time - s.start_time, "text": s.text or ""} for s in pool],
            audio_url, target_language=target_language,
        )
        chosen = [s for s in pool if picks and s.id in set(picks)]
        if not chosen:
            raise RuntimeError(reason or "omni 挑出的行都不在候选里")
        await say("picked", picked=[s.id for s in chosen])
        return presets.plan_from_segments("smart", chosen)

    try:
        if preset == "smart":
            return await ask_omni()
        if preset:
            return presets.build_plan(preset, segments, speaker_id)
        if smart:
            try:
                return await ask_omni()
            except Exception as e:  # 增强不是依赖：失败回退规则
                logger.warning(f"[Clone] omni picker failed ({e}) — using the default preset")
                await say("pick_failed", error=str(e)[:120])
        return presets.default_plan(segments, speaker_id)
    except presets.PresetUnavailable as e:
        raise RuntimeError(f"说话人 {speaker_id} 没有可用于克隆的素材：{e}") from e


def sample_source(video_id: str) -> str:
    """参考音频来源：优先全频带人声（vocals.wav），否则抽出的整条音频。"""
    base = video_dir_path(video_id)
    for name in ("vocals.wav", "extracted_audio.wav"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    raise RuntimeError("没有可用于克隆的音频文件")


def sample_path(video_id: str, speaker_id: str, label: str = "sample") -> str:
    """只读：不建目录。写入处自己建。"""
    return os.path.join(video_dir_path(video_id), "voice_clone", speaker_id, f"{label}.wav")


async def enroll_voice(
    video_id: str,
    speaker_id: str,
    plan: presets.Plan,
    label: str = "sample",
    emit: Optional[Callable] = None,
) -> str:
    """
    拼样本 → 注册音色 → 轮询到就绪，返回 voice_id。每一步都有各自的超时上限
    （一个挂住的请求不能永久占住线程池里的 worker）。

    样本以 data URI 直接交给 DashScope，不再依赖"公网能访问本服务"：旧做法要求
    SERVER_URL_BASE 可达，且 DashScope 拉取失败要重试。本地开发、内网部署都能用。

    新建音色是计费 / 配额动作（0.01 元/个，账号 1000 个免费额度，删除不返还），所以
    记账点只放在"创建成功"这一行。
    """
    async def say(status: str, **kw):
        if emit is not None:
            await emit({"phase": "voice_clone", "status": status, "speaker_id": speaker_id, **kw})

    loop = asyncio.get_running_loop()
    if plan.seconds < 1.0:
        raise RuntimeError(f"说话人 {speaker_id} 的素材只有 {plan.seconds:.1f}s（至少 1s）")

    # —— 拼样本 ——
    speaker_dir = os.path.join(get_video_dir(video_id), "voice_clone", speaker_id)
    os.makedirs(speaker_dir, exist_ok=True)
    out = os.path.join(speaker_dir, f"{label}.wav")
    await say("sampling", seconds=round(plan.seconds, 1), preset=plan.preset)
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, lambda: _ffmpeg_build_sample(sample_source(video_id), plan, out)),
            timeout=config.CLONE_SAMPLE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"参考音频拼接超时（>{config.CLONE_SAMPLE_TIMEOUT}s）") from None

    with open(out, "rb") as f:
        data_uri = "data:audio/wav;base64," + base64.b64encode(f.read()).decode()

    # —— 注册 ——
    prefix = "".join(
        c for c in f"v{video_id[:8]}{speaker_id.replace(' ', '').lower()[:6]}" if c.isalnum()
    )[:10]
    target_model = clone_target_model()
    logger.info(f"[Clone] enrolling {speaker_id}/{label}: model={target_model} prefix={prefix} preset={plan.preset}")

    def _create() -> str:
        return VoiceEnrollmentService().create_voice(
            target_model=target_model, prefix=prefix, url=data_uri,
            max_prompt_audio_length=float(SAMPLE_MAX_DURATION),
        )

    await say("enrolling", timeout_s=config.CLONE_ENROLL_TIMEOUT)
    try:
        voice_id = await asyncio.wait_for(
            loop.run_in_executor(None, _create), timeout=config.CLONE_ENROLL_TIMEOUT
        )
    except asyncio.TimeoutError:
        # asyncio 的超时是"放弃等待"而不是"杀掉那个调用"：线程里的请求会自己跑完，结果
        # 丢弃。消息要说人话，否则日志里只剩一个空白的 TimeoutError。
        raise RuntimeError(
            f"注册音色超时（>{config.CLONE_ENROLL_TIMEOUT}s，DashScope 无响应或正在排队）"
        ) from None
    usage_service.record(
        video_id=video_id, step="clone", model="qwen-voice-enrollment",
        detail=f"{speaker_id}/{label} -> {target_model}", usage={"voices": 1},
    )

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


async def delete_voice(voice_id: str) -> bool:
    """删掉服务端的音色（清理没选用的候选）。失败按"没删成"返回 False，不抛。"""
    try:
        await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None, lambda: VoiceEnrollmentService().delete_voice(voice_id=voice_id)
            ),
            timeout=config.CLONE_POLL_CALL_TIMEOUT,
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[Clone] could not delete {voice_id}: {e}")
        return False


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


def preview_path(video_id: str, label: str, name: str = "preview.mp3") -> str:
    """只读：被 GET 路由调用，所以不能建目录（`get_video_dir` 会 makedirs）。写入处自己建。"""
    return os.path.join(video_dir_path(video_id), "voice_clone", label, name)


async def preview_voice(
    video_id: str, voice: str, model: str, language: str, label: str,
    text: Optional[str] = None, name: str = "preview.mp3",
) -> str:
    """用某个音色（克隆或系统）合成一句话，返回 mp3 路径。不给 `text` 就念固定的试听句（见 voice_presets.PREVIEW_SENTENCES）。"""
    from app.services import tts_service
    text = text or presets.preview_sentence(language)
    path = preview_path(video_id, label, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def _go() -> str:
        audio = tts_service._synth_audio(model, voice, text, video_id=video_id, detail=f"preview({label})")
        if not audio:
            raise RuntimeError("试听合成返回了空音频")
        with open(path, "wb") as f:
            f.write(audio)
        return path

    return await asyncio.get_running_loop().run_in_executor(None, _go)
