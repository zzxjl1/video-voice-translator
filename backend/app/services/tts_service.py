"""
TTS：只做"合成一句话 → 落盘 mp3 → 返回时长"，不碰任何项目状态。

音色、语速、语气指令全部由调用方（命令/任务层）从项目文档里解析好传进来；
配音是否新鲜由 `app/status.py` 的指纹判断。这里没有登记表、没有音色映射文件：
那些旧的旁路状态（tts_results.json / speaker_voices.json）都并进了 project.json。
"""
import asyncio
import logging
import os
import random
import re

import dashscope
from dashscope.audio.tts_v2 import SpeechSynthesizer

from app import config
from app.models import get_video_dir, tts_audio_path
from app.services import usage_service

logger = logging.getLogger(__name__)

dashscope.api_key = config.DASHSCOPE_API_KEY

# segment_id 会被拼进文件路径（tts/{id}.mp3），必须是安全的"单段文件名"：
# 挡住 `../` 之类。ASR 生成的 id 形如 seg-1a2b3c4d。
_SAFE_SEGMENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_segment_id(segment_id: str) -> bool:
    return bool(_SAFE_SEGMENT_ID.match(segment_id or ""))


def pick_voice(language: str | None, taken: set[str]) -> str:
    """
    从能说 `language` 的音色池里选一个，尽量不与 `taken`（别的说话人已用的）重复。

    系统音色 id 同时绑定模型和语言：多数"精品中文"音色只会说普通话，拿去配英文会被
    服务端直接拒绝，所以池子按语言分组。没有任何内置音色支持该语言时仍返回多语种
    池里的一个（拒绝整个任务不如给个差一点的），日志里点名 —— 真正的解法是声音克隆。
    """
    if not config.system_voices_support(language):
        logger.warning(
            f"No built-in voice is documented for '{language}'; using the multilingual "
            f"pool anyway. Expect mispronunciation — voice cloning covers this language."
        )
    pool = config.voices_for_language(language)
    available = [v for v in pool if v not in taken] or list(pool)
    return random.choice(available)


def _clamp_rate(rate: float) -> float:
    """Clamp to the range the API accepts (verified 0.5–2.0)."""
    return max(config.TTS_RATE_MIN, min(config.TTS_RATE_MAX, rate))


def _probe_duration(path: str) -> float | None:
    from app.services.subtitle_service import probe_duration
    return probe_duration(path)


def _synth_audio(
    model: str,
    voice: str,
    text: str,
    *,
    speech_rate: float | None = None,
    instruction: str | None = None,
    video_id: str | None = None,
    detail: str | None = None,
) -> bytes:
    """
    【唯一】构造 SpeechSynthesizer 并调用它的地方：调用与记账绑在一起。

    为什么抽出来：试听（voice_clone_service.preview_cloned_voice）曾经自己 new
    一个 SpeechSynthesizer。同一件事有两份实现，就一定有一份会漏掉后来加的规则
    —— 那份既没有记账，也没有共用的参数处理。

    `instruction`（方言/口音）直接传：SDK 版本由 requirements.txt 锁在
    `dashscope>=1.27.1`，而这个参数正是 1.27.1 引入的 —— 版本已经指定，就不需要
    再写一层运行时探测。
    """
    kwargs: dict = {"model": model, "voice": voice}
    if speech_rate is not None:
        kwargs["speech_rate"] = speech_rate
    if instruction:
        kwargs["instruction"] = instruction

    synthesizer = SpeechSynthesizer(**kwargs)
    audio = synthesizer.call(text)
    response = synthesizer.get_response() or {}
    # 记账放在这里而不是调用方：每个调用方都要自己记得记一次账，就一定会有
    # 一个忘了（试听就是）。用量原样保留服务端返回的 usage。
    usage_service.record(
        video_id=video_id,
        step="tts",
        model=model,
        detail=detail or voice,
        usage=((response.get("payload") or {}).get("usage")) or {},
    )
    return audio


def _synthesize_blocking(
    text: str,
    voice: str,
    speech_rate: float | None = None,
    instruction: str | None = None,
    video_id: str | None = None,
) -> bytes:
    """
    One blocking synthesis call (pipeline / manual re-synthesis path).

    `speech_rate` is only sent when the caller provided one: the API's default
    is 1.0 and an unset value keeps the previous behaviour exactly (important
    for the manual re-synthesis and preview paths).

    `instruction` is how a Chinese dialect is requested — Qwen-Audio-TTS has no
    accent parameter. Also omitted when unset, for the same reason as
    `speech_rate`. It is already validated by `config.tts_instruction`; this
    layer only forwards it.
    """
    return _synth_audio(
        config.TTS_MODEL,
        voice,
        text,
        speech_rate=speech_rate,
        instruction=instruction,
        video_id=video_id,
    )


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
            # 超时保护：卡住的调用必须变成"这一次失败"，而不是"整条管线永久卡住"。
            # 线程里的阻塞调用没法真正取消（SDK 不提供），但至少这一批能往下走，
            # 用户能看到失败原因并重试。
            audio = await asyncio.wait_for(
                loop.run_in_executor(
                    None, _synthesize_blocking, text, voice, speech_rate, instruction, video_id
                ),
                timeout=config.TTS_CALL_TIMEOUT,
            )
            if audio:
                return audio
            last_error = RuntimeError("TTS returned empty audio")
        except asyncio.TimeoutError:
            last_error = RuntimeError(
                f"合成超时（>{config.TTS_CALL_TIMEOUT}s，网络或服务端无响应）"
            )
        except TypeError as e:
            # 参数/签名错是**确定性**的：重试改变不了 SDK 的签名，只会把同一条
            # 日志刷 TTS_MAX_RETRIES+1 遍并拖慢整批（实测线上就是这个现象）。
            logger.error(f"TTS cannot run for {segment_id} (SDK/参数不兼容): {e}")
            last_error = e
            break
        except Exception as e:
            last_error = e

        if attempt <= config.TTS_MAX_RETRIES:
            logger.warning(
                f"TTS attempt {attempt} failed for {segment_id}: {last_error}. Retrying..."
            )
            await asyncio.sleep(1.0 * attempt)

    # 不带 "TTS failed for segment X" 前缀：调用方（pipeline / 手动重合成）自己
    # 会加上下文，两层都带就成了
    # "... for segment seg-x: TTS failed for segment seg-x: <真原因>"（实测日志）。
    attempts = min(attempt, config.TTS_MAX_RETRIES + 1)
    raise RuntimeError(f"{last_error}（尝试 {attempts} 次）")


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


# 标签的识别与规范化统一在 config（同一套定义还要给"译文标签比对"用）。
_TAG_RE = config.EMOTION_TAG_RE
_canonical_tag = config.canonical_emotion_tag
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_for_tts(text: str, segment_id: str) -> str:
    """
    送合成前的最后一道防线：只放行白名单标签。

    上游（omni 标注）不再做任何净化 —— 它产出什么就送什么，这样标签问题在
    enhanced_transcript.json 里可观察。但 TTS 是边界：白名单之外的标记会被
    当正文读出来（比如它自造的 [mad]），这条防线只拦这个。上限沿用原有
    约定（控制类每句 1、富语言每句 2）——多出来的同类标签对合成本来就没
    有增量意义。

    三种写法在这里被**归一**成合成器认的形态，而不是被剥掉：
      * 全角 【excited】 → [excited]
      * 大小写  [Excited] → [excited]
      * 多空格  [very  slowly] → [very slowly]
    只有归一之后仍不在白名单里的（自造标签、歧义括号内容）才剥除 ——
    剥除一律打日志，让"防线拦了什么"可追查而不是静默丢失。
    """
    if "[" not in text and "【" not in text:
        return text

    kept_control = kept_rich = 0
    stripped: list[str] = []

    def _keep(m: re.Match) -> str:
        nonlocal kept_control, kept_rich
        tag = _canonical_tag(m.group(1))
        if tag in config.EMOTION_CONTROL_TAGS and kept_control < config.EMOTION_MAX_CONTROL_PER_LINE:
            kept_control += 1
            return tag
        if tag in config.EMOTION_RICH_TAGS and kept_rich < config.EMOTION_MAX_RICH_PER_LINE:
            kept_rich += 1
            return tag
        stripped.append(m.group(0))
        return ""

    out = _TAG_RE.sub(_keep, text)

    if not stripped:
        # 没有任何标签被剥除。但**可能发生过归一**（全角/大小写/多空格）——
        # 那时必须返回 `out`：以前这里直接 `return text`，于是"归一"算了却
        # 丢掉，【excited】照样原样送去合成（合成器只认 [excited]）。
        if out != text:
            logger.info(
                f"[TTS] {segment_id}: normalized tag spelling for synthesis: "
                f"{out[:80]}"
            )
            return out
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
    voice: str,
    speech_rate: float | None = None,
    target_duration: float | None = None,
    instruction: str | None = None,
) -> tuple[str, float | None]:
    """
    合成一句话，写到 tts/<segment_id>.mp3，返回 (路径, 实际时长秒)。

    Args:
        voice: 已解析好的音色（本行 > 克隆 > 说话人的系统音色，见 status.resolve_voice）。
        speech_rate: 语速 0.5–2.0；None 用默认。
        target_duration: 这一行要填满的时间槽。给了就会量结果，偏差超过
            `config.TTS_FIT_TOLERANCE` 时按修正后的语速重合成一次（多一次调用，
            只花在需要的行上）。
        instruction: 已合并好的语气/方言指令（status.dub_instruction）。拟合步骤也要
            带上它，否则被重合成的那一行会变回普通话/平淡语气，而别的行不是。

    时长必须由调用方落进项目文档：混音计划用它算每段的 atempo。
    """
    if not is_safe_segment_id(segment_id):
        raise ValueError(f"invalid segment id: {segment_id!r}")
    if not voice:
        raise ValueError("no voice resolved for this line")

    text = _sanitize_for_tts(text, segment_id)
    logger.info(
        f"Submitting TTS task to DashScope ({config.TTS_MODEL}), Voice: {voice}, "
        f"rate: {speech_rate if speech_rate is not None else 'default'}"
        f"{', instruction: ' + instruction if instruction else ''} for {segment_id}"
    )

    loop = asyncio.get_running_loop()
    # SDK 的调用是阻塞的 → 放进线程池，事件循环和其它并发的合成任务照常推进。
    audio = await _synthesize_with_retries(
        loop, text, voice, speech_rate, segment_id, instruction, video_id
    )

    get_video_dir(video_id)
    audio_path = tts_audio_path(video_id, segment_id)
    os.makedirs(os.path.dirname(audio_path), exist_ok=True)

    # 写盘失败必须抛：吞掉的话调用方会把这一行记成功，界面显示"已配音"而实际没有
    # 文件，导出时又被静默跳过。
    actual, applied = await _write_fitted_audio(
        loop, audio_path, audio, text, voice, speech_rate, target_duration,
        segment_id, video_id, instruction,
    )
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise RuntimeError(f"合成结果没有落盘：{audio_path}")
    detail = f", {actual:.2f}s" if actual else ""
    detail += f" @ rate {applied:.2f}" if applied else ""
    logger.info(f"Saved synthesized audio to {audio_path}{detail}")
    return audio_path, actual
