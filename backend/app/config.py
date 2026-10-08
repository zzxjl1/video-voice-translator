"""
Application configuration.

All secrets are read from environment variables. Put them in `backend/.env`
(see `backend/.env.example`), which is git-ignored.

Nothing in this module should ever hardcode an API key.
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# ----- Paths -----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(BASE_DIR, ".env")


def _load_dotenv(path: str) -> None:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Uses python-dotenv when installed, otherwise falls back to a minimal
    parser so the app still boots without the extra dependency.
    Existing environment variables always win (override=False).
    """
    if not os.path.exists(path):
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Failed to parse %s: %s", path, e)


_load_dotenv(ENV_PATH)


# ----- Env helpers -----

_missing_secrets: list[str] = []


def _secret(name: str) -> str:
    """Read a required secret. Missing values are reported all at once below."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        _missing_secrets.append(name)
    return value


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning("Invalid integer for %s=%r, using %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        logger.warning("Invalid float for %s=%r, using %s", name, raw, default)
        return default


# =====================================================================
# Secrets (required)
# =====================================================================

# ----- Ali DashScope (ASR + TTS + voice cloning) -----
DASHSCOPE_API_KEY = _secret("DASHSCOPE_API_KEY")

# ----- LLM provider (OpenAI-compatible, e.g. SiliconFlow) -----
LLM_API_KEY = _secret("LLM_API_KEY")

if _missing_secrets:
    raise RuntimeError(
        "Missing required secret(s): "
        + ", ".join(sorted(set(_missing_secrets)))
        + ".\nCreate the file:\n"
        + f"    {ENV_PATH}\n"
        + "by copying the template:\n"
        + f"    cp {ENV_PATH}.example {ENV_PATH}\n"
        + "then fill in real values. Never commit .env to git."
    )


# =====================================================================
# LLM / translation
# =====================================================================

# Defaults target DeepSeek's official API. Any OpenAI-compatible endpoint works
# (SiliconFlow, DashScope's compatible mode, …); see .env.example.
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = _env("LLM_MODEL", "deepseek-flash")
# Provider-specific passthrough. It is a SiliconFlow/Qwen knob; DeepSeek accepts
# and ignores it, so it is harmless either way.
LLM_THINKING_ENABLED = _env_bool("LLM_THINKING_ENABLED", False)
LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 180)
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.3)

# Reasoning models spend part of the completion budget on hidden "thinking"
# before writing the answer, so the cap has to cover both. Leave this explicit:
# with the provider default a long chunk can be cut off mid-JSON. `deepseek-flash`
# accepts 8192 (verified).
LLM_MAX_TOKENS = _env_int("LLM_MAX_TOKENS", 8192)

# Ask the provider to guarantee a JSON object in `content`. Everything reading
# these responses parses JSON, so this removes a whole class of "the model
# wrapped the array in prose" failures. Supported by DeepSeek and most
# OpenAI-compatible gateways (verified on DeepSeek).
LLM_JSON_MODE = _env_bool("LLM_JSON_MODE", True)

# Optional reasoning control. A reasoning model emits `reasoning_content` before
# the answer; translation does not need it, and it costs tokens and latency.
# Verified on deepseek-flash: "none" removes the reasoning block entirely.
# Blank = send nothing and accept the provider default. Common values: none /
# minimal / low / medium / high.
LLM_REASONING_EFFORT = _env("LLM_REASONING_EFFORT", "")

# Chunking is DERIVED from the model's own limits rather than hand-tuned. The
# old pair (25 segments / 4000 chars) was sized for a small window and cut long
# scripts into pieces the model then had to understand one at a time.
#
# Verified 2026-10-07 against DeepSeek's docs: `deepseek-flash` has a
# 1M-token context and a 384K output cap, and its price does NOT step up with
# input length — so one request can hold a whole script, and doing so is cheap.
LLM_CONTEXT_TOKENS = _env_int("LLM_CONTEXT_TOKENS", 1_000_000)
# Fraction of the window one request may use; the rest is headroom.
LLM_CHUNK_CONTEXT_USAGE = _env_float("LLM_CHUNK_CONTEXT_USAGE", 0.8)
# Fixed per-request cost: rules + length budget + glossary + JSON format block +
# the context tail. Measured on a real run: ~1.7K chars of scaffolding.
LLM_CHUNK_RESERVE_TOKENS = _env_int("LLM_CHUNK_RESERVE_TOKENS", 6000)
# The answer shares `max_tokens` with the model's hidden thinking, and with
# provider defaults that thinking is ON: a measured one-line translation spent
# 208 of its 223 output tokens on reasoning. Setting LLM_REASONING_EFFORT=none
# removes that entirely; this allowance keeps chunks honest while it is on.
LLM_CHUNK_REASONING_ALLOWANCE_TOKENS = _env_int("LLM_CHUNK_REASONING_ALLOWANCE_TOKENS", 8000)
LLM_CHUNK_OUTPUT_RESERVE_TOKENS = _env_int("LLM_CHUNK_OUTPUT_RESERVE_TOKENS", 2000)
# Token estimates, deliberately pessimistic (see _estimate_segment_tokens).
LLM_SOURCE_CHARS_PER_TOKEN = _env_float("LLM_SOURCE_CHARS_PER_TOKEN", 1.5)
# Deliberately NO segment-count cap: a script is split by token usage alone, so
# how many lines a request may carry is a consequence of the budget, never a
# separate limit someone has to keep in sync with it.
LLM_CHUNK_CONTEXT_SEGMENTS = _env_int("LLM_CHUNK_CONTEXT_SEGMENTS", 3)
LLM_CHUNK_MAX_RETRIES = _env_int("LLM_CHUNK_MAX_RETRIES", 2)

# Extract a glossary (names, jargon, product terms) from the full source script
# before translating, so terminology stays consistent across chunks.
GLOSSARY_AUTO_EXTRACT = _env_bool("GLOSSARY_AUTO_EXTRACT", True)
GLOSSARY_MAX_TERMS = _env_int("GLOSSARY_MAX_TERMS", 40)
# Optional per-video glossary file name inside the video directory.
GLOSSARY_FILE = _env("GLOSSARY_FILE", "glossary.json")

# Speaking-rate budgets, used to ask the model to fit each line into the time
# slot it has to fill, so the TTS output does not have to be time-stretched on
# playback.
#
# ALL OF THESE ARE MEASURED, not estimated. Each value comes from synthesizing
# sample lines with the voice this project actually uses and reading the result
# back with ffprobe. The previous numbers were guesses — Chinese was 4.5 where
# the real figure is 5.15, so every budget was ~14% too tight and lines came out
# shorter than their slot.
#
# Units: characters/sec for CJK, words/sec for latin scripts (this is the unit
# the LLM reasons in, and it is what `maxLength` is expressed in).
SPEECH_RATE_VALUE = {
    "Chinese": 5.65,
    "Japanese": 7.17,
    "Korean": 6.5,
    "English": 2.98,
    "French": 2.45,
    "German": 2.5,
    "Spanish": 2.9,
}
DEFAULT_SPEECH_RATE_VALUE = 2.9

# The same measurement expressed in "spoken units" — roughly syllables, as
# counted by `speech_timing.speech_units()`. This is what the TTS duration
# prediction uses, because a character count is a bad proxy: "Python" is 6
# characters but 2 units, "73%" is 3 characters but ~8 units.
SPEECH_UNITS_PER_SECOND = {
    "Chinese": 5.07,
    "Japanese": 6.5,
    "Korean": 6.06,
    "English": 4.46,
    "French": 4.47,
    "German": 4.07,
    "Spanish": 4.34,
}
DEFAULT_SPEECH_UNITS_PER_SECOND = 4.4

# A line costs a fixed amount of time no matter how short it is (lead-in and
# trailing pause). Measured: a 3-character Chinese line still takes 0.78s.
#
#     duration ~= units / units_per_second + TTS_FIXED_OVERHEAD
#
# Ignoring this is why short lines were predicted badly. Fitted per language it
# ranged 0.04–0.61s across a noisy 4-sample set, so a single shared value is
# used instead — the slopes are what carry the signal.
TTS_FIXED_OVERHEAD = _env_float("TTS_FIXED_OVERHEAD", 0.3)

def speech_rate_value(language: str) -> float:
    """Numeric speaking rate (chars or words per second)."""
    return SPEECH_RATE_VALUE.get(language, DEFAULT_SPEECH_RATE_VALUE)


def speech_units_per_second(language: str) -> float:
    """Spoken units (≈syllables) per second for this language's voice."""
    return SPEECH_UNITS_PER_SECOND.get(
        language, DEFAULT_SPEECH_UNITS_PER_SECOND
    )


# =====================================================================
# ASR (Ali DashScope, async file transcription)
# =====================================================================

# Model Studio's own ASR guide recommends `qwen-audio-3.1-asr-flash-filetrans`
# for "who said what" (its exact wording), and it is the current generation:
# 30 languages + 10 Chinese dialects, <=12h / <=2GB per file, diarization on.
ASR_MODEL = _env("ASR_MODEL", "qwen-audio-3.1-asr-flash-filetrans")

# Verified against Alibaba Cloud Model Studio docs (non-realtime ASR):
# `diarization_enabled` is supported by Fun-ASR, Paraformer series and
# Qwen-Audio-3.x-ASR-Flash-Filetrans. Qwen3-ASR-Flash is NOT documented to
# support it. Note that diarization also requires MONO audio and <=2h.
ASR_DIARIZATION_MODELS = {
    m.strip()
    for m in _env(
        "ASR_DIARIZATION_MODELS",
        "fun-asr,fun-asr-mtl,paraformer-v2,paraformer-8k-v2,"
        "qwen-audio-3.1-asr-flash-filetrans,qwen-audio-3.0-asr-flash-filetrans",
    ).split(",")
    if m.strip()
}
# 说话人分离没有全局开关：总是请求分离，模型不支持时由 _build_attempt_plan
# 自动降级（同模型不带分离 → 备用模型带分离）。曾经有个
# ASR_DIARIZATION_ENABLED 环境变量，一关就把所有说话人悄悄并成一个，界面
# 上只会显示"说话人 1"，而没有任何地方能看出这是被配置关掉的。
# Used when the configured model rejects `diarization_enabled`. Deliberately a
# different model family so a provider-side outage of one line still works.
ASR_FALLBACK_MODEL = _env("ASR_FALLBACK_MODEL", "fun-asr")

# What the ASR model can hear, from the Model Studio model-comparison page.
#
# Listed here rather than derived anywhere in the client for the same reason as
# SYSTEM_VOICE_LANGUAGES: it is a property of the model, the landing page shows
# it before a video exists, and the last time this knowledge was duplicated the
# UI offered a language no voice could speak.
#
# Note the asymmetry with the TTS side: 30 languages can be HEARD, 10 can be
# SAID by a built-in voice. That gap is why the landing page shows both lists —
# a user with, say, a Greek video needs to know up front that the transcription
# will work but the dub needs voice cloning.
ASR_SOURCE_LANGUAGES = (
    "中文", "英语", "日语", "韩语", "越南语", "泰语", "印尼语", "马来语",
    "菲律宾语", "印地语", "阿拉伯语", "法语", "德语", "西班牙语", "葡萄牙语",
    "俄语", "意大利语", "荷兰语", "瑞典语", "丹麦语", "芬兰语", "挪威语",
    "希腊语", "波兰语", "捷克语", "匈牙利语", "罗马尼亚语", "保加利亚语",
    "克罗地亚语", "斯洛伐克语",
)

# The Chinese dialects/accents ASR distinguishes, listed separately because they
# are not "languages" in the same sense — a Cantonese video is still Chinese.
ASR_CHINESE_DIALECTS = (
    "上海", "南昌", "宁波", "客家", "杭州", "温州", "湖南", "福建", "粤语", "苏州",
)

ASR_SERVICE_URL = _env(
    "ASR_SERVICE_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
)
ASR_TASK_URL_TEMPLATE = _env(
    "ASR_TASK_URL_TEMPLATE", "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"
)
ASR_POLL_INTERVAL = _env_float("ASR_POLL_INTERVAL", 2.0)
ASR_MAX_WAIT = _env_int("ASR_MAX_WAIT", 600)


# =====================================================================
# TTS (Ali DashScope Qwen-Audio-TTS)
# =====================================================================
#
# Model choice constrains the voices in two ways:
#
#   1. System voice IDs are MODEL-SPECIFIC. For `qwen-audio-3.1-tts-flash`
#      every system voice carries a `_v3.1` suffix and IDs are
#      case-sensitive. Reusing a CosyVoice voice (e.g. `longanyang`) with this
#      model fails with `InvalidParameter` / `Engine error [411]`.
#   2. Voice samples must match the model's language support. Most of the
#      "精品中文" voices only synthesize Mandarin, so an English dub assigned
#      one of them fails. That is why the pool below is grouped by language
#      instead of being one flat list.
#
# Cloned voices are bound to the model they were enrolled against and cannot
# be used with a different model (official docs). `voice_clone_service` stores
# the model alongside each voice id so a model switch invalidates the cache
# instead of silently shipping broken audio.
TTS_MODEL = _env("TTS_MODEL", "qwen-audio-3.1-tts-flash")
TTS_TIMEOUT = _env_int("TTS_TIMEOUT", 60)

# ---- 多模态增强识别：omni 听原声，审核纠错 ASR 结果并标注语气 ----
# 开启后，ASR 转写与原音频一起送 omni 模型二次处理：
#   1. 纠正识别错误（同音字、错词、漏标点）—— 以听到的为准；
#   2. 给每句标注情感控制标签（[excited] 等）与拟声标签（[laughing] 等）。
# 纠错+标注后的文本作为翻译输入（DeepSeek 的提示词会要求原样保留标签），
# 译文进 TTS 时标签被渲染成语气。标注结果随工程落盘，恢复时直接复用。
#
# 这里【没有】全局开关：唯一的控制是工程里那个可见开关（「多模态增强识别」，
# 随工程持久化，默认关）。曾经有个 MULTIMODAL_ENHANCE 环境变量，但没有任何
# 代码读它 —— 一个叫得像总闸门、实际什么都不做的配置，比没有更糟。
OMNI_MODEL = _env("OMNI_MODEL", "qwen3.8-omni-flash")
OMNI_BASE_URL = _env("OMNI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
# 白名单 = TTS 文档列出的标签（2026-10）。omni 返回的标签凡不在表内一律丢弃。
# 情感/富语言标签 —— 白名单必须与官方文档一致。
#
# ⚠️ 只有这三个模型认这些标签（官方文档）：

#   qwen-audio-3.1-tts-flash（TTS_MODEL 默认值）/ qwen-audio-3.0-tts-plus /
#   qwen-audio-3.0-tts-flash。换成别的 TTS 模型时，标签不但无效，还会被当正文
#   念出来 —— 那样必须同时关掉多模态增强（mm_enhance），或改写这条链路。
#
# 漏掉任何一个官方标签的后果是**功能被静默剥掉**：`tts_service._sanitize_for_tts`
# 会把白名单之外的一切当噪音删除（自造标签、被翻成中文的标签都走这条路），
# 所以清单短一个，模型按文档写的标签就少一个。
EMOTION_CONTROL_TAGS = [
    "[sad]", "[amazed]", "[deep and loud shouting]", "[trembling]", "[angry]",
    "[excited]", "[sarcastic]", "[curious]", "[like dracula]", "[bored]",
    "[tired]", "[scornful]", "[shouting]", "[asmr]", "[panicked]",
    "[mischievously]", "[empathetic]", "[whispers]", "[reluctantly]", "[crying]",
    "[serious]", "[very slowly]", "[very fast]",
]
EMOTION_RICH_TAGS = [
    "[gasp]", "[sighing]", "[clears throat]", "[giggles]", "[laughing]",
    "[cough]", "[snorts]",
]
import re as _re

# 识别文本里的情感/富语言标签。**两种括号都认**：全角 【…】 是模型（尤其中文
# 语境）常见的输出，而合成器只认半角 [tag]；只认半角会让【大笑】被当正文念出来。
EMOTION_TAG_RE = _re.compile(r"[\[【]([^\[\]【】]{1,32})[\]】]")


def canonical_emotion_tag(inner: str) -> str:
    """
    标签名的规范写法：半角方括号 + 去多余空白的小写名。

    大小写与空格都不该让功能被静默丢掉：`[Excited]`、`[very  slowly]`、
    `【Excited】` 与 `[excited]`、`[very slowly]` 是同一个标签，而合成器只认后者。
    tts_service（合成前归一）与 llm_service（译文标签比对）共用这一处定义。
    """
    return f"[{' '.join(inner.strip().lower().split())}]"


# 每句最多 1 个控制标签 + 2 个富语言标签，再多就成了音效串烧。
EMOTION_MAX_CONTROL_PER_LINE = 1
EMOTION_MAX_RICH_PER_LINE = 2

# ---- 智能选材：omni 听音频挑克隆参考段 ----
# 由 omni 模型通过 tool call 从候选段里挑选参考素材，每轮选择都校验（id 归属 /
# 时长 / 重叠），最多 MAX_TURNS 轮，失败回退规则。
#
# 这里【没有】全局闸门：唯一的控制是工程里那个可见开关（VideoUpload 的
# 「智能选材」，随工程持久化，默认关）。曾经有过一个默认 False 的
# CLONE_OMNI_PICKER 环境开关，效果是用户打开界面开关、以为生效了、实际每次
# 都在走规则选材，且界面上没有任何提示 —— 一个看不见的第二控制权不该存在。
CLONE_OMNI_MAX_TURNS = _env_int("CLONE_OMNI_MAX_TURNS", 5)
# 克隆链路三处阻塞调用的上限（P1 #12）：本地 ffmpeg 拼接、DashScope create_voice、
# 每次 query_voice 轮询。三者以前都是裸调用 —— 一个卡住的请求就永久占住整条管线
# 与 run 注册表（/process 与 DELETE 从此都是 409，用户无法自救）。
CLONE_SAMPLE_TIMEOUT = _env_int("CLONE_SAMPLE_TIMEOUT", 120)
CLONE_ENROLL_TIMEOUT = _env_int("CLONE_ENROLL_TIMEOUT", 180)
CLONE_POLL_CALL_TIMEOUT = _env_int("CLONE_POLL_CALL_TIMEOUT", 30)

# Number of concurrent synthesis calls. This is I/O bound (blocking SDK call
# runs in a thread pool) so it does not increase CPU usage. Raise it only if
# the provider does not rate-limit you.
TTS_CONCURRENCY = _env_int("TTS_CONCURRENCY", 4)
TTS_MAX_RETRIES = _env_int("TTS_MAX_RETRIES", 2)
# 单次合成的墙钟上限：SDK 的 call() 是阻塞的网络调用，以前完全没有超时 ——
# 一个卡住的 websocket 就能让整条管线永久停在 TTS 阶段（run 也永远占位，
# 连 DELETE 都是 409）。超时按"可重试的失败"处理，最坏 3 次尝试 × 这个值。
TTS_CALL_TIMEOUT = _env_int("TTS_CALL_TIMEOUT", 60)

# Audio format used for the synthesized files and the export timeline.
# The SDK binds the sample rate to its `format` enum (default mp3 22.05 kHz);
# export re-samples with ffmpeg, so this only sizes the mixing timeline.
TTS_SAMPLE_RATE = _env_int("TTS_SAMPLE_RATE", 24000)

# ----- Duration fitting -----
# Each line is synthesized once and then measured with ffprobe (free). If the
# result misses its time slot by more than the tolerance, it is re-synthesized
# once at a corrected `speech_rate`.
#
# Tolerance is a listenability budget, NOT an accuracy target: up to ~20% of
# pitch-preserved stretching in the browser is inaudible, so correcting tighter
# than that just burns TTS calls. Above it the stretch becomes audible, so a
# native re-synthesis is worth the extra call.
TTS_FIT_TOLERANCE = _env_float("TTS_FIT_TOLERANCE", 0.20)
# `speech_rate` bounds accepted by the API (verified 0.5–2.0).
TTS_RATE_MIN = _env_float("TTS_RATE_MIN", 0.5)
TTS_RATE_MAX = _env_float("TTS_RATE_MAX", 2.0)

# `qwen-audio-3.1-tts-flash` voices, grouped by supported language.
# Source: Model Studio "Qwen-Audio-TTS voice list". These four handle
# Mandarin plus 8 dialects and 8 foreign languages, so they are the safe
# choice for anything that is not plain Chinese or English.
TTS_MULTILINGUAL_VOICES = [
    "longanhuan_v3.1",      # female
    "longanlingxin_v3.1",   # female
    "longanfengyue_v3.1",   # female
    "xunanchuan_v3.1",      # male
]

# 精品中文: Mandarin only.
_TTS_VOICES_ZH = [
    "yuxiaoyun_v3.1",
    "qiaoxiaojiao_v3.1",
    "xiaxiaochen_v3.1",
    "anmingyuan_v3.1",
    "wenhuaiqing_v3.1",
    "anxiaolan_v3.1",
    "xieshurou_v3.1",
    "baiqinglan_v3.1",
    "xuyuyuan_v3.1",
    "anruorou_v3.1",
    "wenhuaizhi_v3.1",
    "xiaoxingzhi_v3.1",
    "guyunshu_v3.1",
    "huozhuoshi_v3.1",
    "yeqinghe_v3.1",
    "yunhuanhuan_v3.1",
    "xuxiaoqiao_v3.1",
    "baianran_v3.1",
    "xuyanchu_v3.1",
    "yezhiqing_v3.1",
    "andi_v3.1",
    "anyuqing_v3.1",
]

# 精品英文: English only.
_TTS_VOICES_EN = [
    "Emily_v3.1",
    "Luna_v3.1",
    "Eric_v3.1",
    "Luca_v3.1",
    "Abby_v3.1",
    "Annie_v3.1",
    "Ava_v3.1",
    "Beth_v3.1",
    "Betty_v3.1",
    "Cally_v3.1",
    "Cindy_v3.1",
    "Donna_v3.1",
    "Andy_v3.1",
    "Brian_v3.1",
    "David_v3.1",
]

TTS_VOICES_BY_LANGUAGE: dict[str, list[str]] = {
    "Chinese": _TTS_VOICES_ZH + TTS_MULTILINGUAL_VOICES,
    "English": _TTS_VOICES_EN,
    # The multilingual set covers these; no per-language "精品" voices exist.
    "Japanese": TTS_MULTILINGUAL_VOICES,
    "Korean": TTS_MULTILINGUAL_VOICES,
    "French": TTS_MULTILINGUAL_VOICES,
    "German": TTS_MULTILINGUAL_VOICES,
    "Portuguese": TTS_MULTILINGUAL_VOICES,
    "Italian": TTS_MULTILINGUAL_VOICES,
    "Vietnamese": TTS_MULTILINGUAL_VOICES,
    "Indonesian": TTS_MULTILINGUAL_VOICES,
    # Deliberately NO entry for Spanish — nor for Russian, Thai, Malay,
    # Filipino, Arabic and the rest. No built-in voice speaks them, and listing
    # one anyway is exactly how the UI came to offer Spanish with a voice that
    # mispronounced every line while nothing reported a problem.
    #
    # Those languages are still reachable, through voice CLONING, which is
    # documented for 16 foreign languages. That is the only honest way to offer
    # them: with a cloned voice, not with a system voice that cannot do it.
    # See SYSTEM_VOICE_LANGUAGES.
}

# The languages a SYSTEM voice can actually speak.
#
# Exactly what the four multilingual voices are documented for, plus Mandarin
# and English which have their own dedicated pools. Everything else the ASR
# model can HEAR needs voice CLONING to be SAID — there simply is no built-in
# voice for it.
#
# This matters because the two directions have different coverage: the ASR
# model understands 30 languages, while the system voices can only speak these
# 10. A language outside this set is not an error — it is a "turn on voice
# cloning", and saying so is the difference between a quality problem and a
# silent one. The docs describe an unsupported pair as
# 「可能发音错误或语音不自然」: it produces audio, badly.
SYSTEM_VOICE_LANGUAGES = frozenset(
    {
        "Chinese",
        "English",
        "Japanese",
        "Korean",
        "French",
        "German",
        "Portuguese",
        "Italian",
        "Vietnamese",
        "Indonesian",
    }
)


def system_voices_support(language: Optional[str]) -> bool:
    """
    Whether any built-in voice can speak `language`.

    False means the synthesizer has nothing valid to offer and the job needs
    voice cloning — NOT that nothing can be done. `voices_for_language` still
    returns a pool for such a language, because refusing outright would fail a
    run that the user can still salvage; the pool just is not a good one.
    """
    return bool(language) and language in SYSTEM_VOICE_LANGUAGES

# Fallback pool / manual override. Defaults to the multilingual set because it
# is the only one that is safe for an unknown target language.
TTS_VOICES = [
    v.strip()
    for v in _env("TTS_VOICES", ",".join(TTS_MULTILINGUAL_VOICES)).split(",")
    if v.strip()
]
TTS_DEFAULT_VOICE = _env("TTS_DEFAULT_VOICE", "longanhuan_v3.1")


# ----- Chinese accents (TTS `instruction`) -----
#
# Qwen-Audio-TTS has no accent parameter. Dialect output is requested through
# `instruction` on the synthesizer — the SDK documents it as "the instruction of
# the synthesizer, max length is 128", and the docs' own example is
# 「请用河南话表达。」.
#
# The set below is the eight dialects the four multilingual SYSTEM voices are
# documented to speak. Deliberately NOT the 23-dialect list from the voice
# cloning page: that one needs cloning enabled to mean anything, and listing it
# would offer 15 dialects that quietly come out as Mandarin.
#
# Membership is checked here rather than trusted from the client, because the
# value is interpolated into a prompt. A stale or hand-made request naming
# something else is dropped, not forwarded.
CHINESE_ACCENTS = (
    "广东话",
    "上海话",
    "东北话",
    "重庆话",
    "陕西话",
    "云南话",
    "宁波话",
    "甘肃话",
)


def tts_instruction(target_language: str | None, accent: str | None) -> str | None:
    """
    The `instruction` to send with a synthesis call, or None for plain speech.

    Mandarin is the ABSENCE of an instruction, not an instruction saying so: the
    default voices already speak it, and a no-op sentence is one more thing the
    service can reject or misread. Accents apply to Chinese only — the other
    languages have no documented dialect control.
    """
    if not accent or target_language != "Chinese":
        return None
    if accent not in CHINESE_ACCENTS:
        return None
    return f"请用{accent}表达。"


# Display names, from the "Qwen-Audio-TTS 音色列表" page. Only the pools the UI
# can actually reach need entries; anything missing falls back to the raw id,
# which is ugly but never wrong.
#
# The English voices are named after the voice itself (`Emily_v3.1` -> "Emily"),
# so they need no entry.
VOICE_LABELS = {
    # 精品中文
    "yuxiaoyun_v3.1": "于小云",
    "qiaoxiaojiao_v3.1": "乔小娇",
    "xiaxiaochen_v3.1": "夏小晨",
    "anmingyuan_v3.1": "安明远",
    "wenhuaiqing_v3.1": "温怀清",
    "anxiaolan_v3.1": "安小岚",
    "xieshurou_v3.1": "谢舒柔",
    "baiqinglan_v3.1": "白清岚",
    "xuyuyuan_v3.1": "许玉远",
    "anruorou_v3.1": "安若柔",
    "wenhuaizhi_v3.1": "闻怀之",
    "xiaoxingzhi_v3.1": "萧行之",
    "guyunshu_v3.1": "顾云舒",
    "huozhuoshi_v3.1": "霍拙石",
    "yeqinghe_v3.1": "叶清禾",
    "yunhuanhuan_v3.1": "云欢欢",
    "xuxiaoqiao_v3.1": "徐小俏",
    "baianran_v3.1": "白安然",
    "xuyanchu_v3.1": "许言初",
    "yezhiqing_v3.1": "叶知晴",
    "andi_v3.1": "安迪",
    "anyuqing_v3.1": "安语晴",
    # 多语言 — 这 4 个是唯一能说外语的系统音色，界面里必须认得出它们
    "longanhuan_v3.1": "龙安欢",
    "longanlingxin_v3.1": "龙安灵心",
    "longanfengyue_v3.1": "龙安风悦",
    "xunanchuan_v3.1": "许南川",
}


def voice_label(voice: str) -> str:
    """Human name for a voice id."""
    if voice in VOICE_LABELS:
        return VOICE_LABELS[voice]
    # `Emily_v3.1` -> `Emily`; leaves anything unexpected untouched.
    return voice.split("_v3")[0] if "_v3" in voice else voice


def voices_for_language(language: Optional[str]) -> list[str]:
    """
    Voice pool that is actually able to synthesize `language`.

    Falls back to the configured `TTS_VOICES` pool when the language is unknown,
    which prevents assigning (say) a Mandarin-only voice to an English dub —
    that combination is rejected by the service rather than gracefully ignored.
    """
    if not language:
        return TTS_VOICES
    return TTS_VOICES_BY_LANGUAGE.get(language) or TTS_VOICES


# =====================================================================
# Vocal separation
# =====================================================================
#
# One real backend. There is deliberately NO self-hosted (PyTorch) option:
# `audio-separator` needs hundreds of MB of RSS and a GPU to be usable, and
# this deployment has neither. Do not add it back.
#
#   "client" — the BROWSER runs the MDX-Net ONNX model (WebGPU/WASM) and
#              uploads the resulting stems. No server CPU/RAM cost, no API
#              cost. Default.
#   "off"    — never separate.
#
# The 302.AI ("api") backend was removed on 2026-10-08: a paid third-party
# dependency whose published hosts are unreliable from mainland networks, while
# the browser backend covers the same need for free. Do not add it back.
#
# `SEPARATION_MODE` is only the DEFAULT the UI starts from. The user picks a
# backend per job and the choice travels in `ProcessRequest.separation_mode`;
# the backend validates it rather than trusting the client.
SEPARATION_MODE = _env("SEPARATION_MODE", "client").lower()

# Initial state of the "BGM Separation" control in the UI.
ENABLE_BGM_SEPARATION_DEFAULT = _env_bool("ENABLE_BGM_SEPARATION_DEFAULT", False)

# ----- Browser ("client") backend -----
# The MDX-Net model served to the browser; its DSP parameters are mirrored to
# the client via GET /api/models/separator so the JS never hardcodes them.
MODELS_DIR = os.path.join(BASE_DIR, "models")
SEPARATOR_MODEL_FILE = _env("SEPARATOR_MODEL_FILE", "UVR-MDX-NET-Inst_HQ_3.onnx")

# 这里【没有】第三方分离后端：302.AI（"api" 模式）已于 2026-10-08 整体移除，
# 相关配置（SEPARATION_API_* 与为它折算美元价格而入的 USD_CNY_RATE）一并删除。

SEPARATION_MODE = SEPARATION_MODE if SEPARATION_MODE in ("client", "off") else "client"
if SEPARATION_MODE != _env("SEPARATION_MODE", "client").lower():
    # .env 里残留 "api"（302.AI，已移除）时不该把它发给前端 —— 那会变成一个
    # 界面上不存在的后端选项。
    logger.warning(
        "SEPARATION_MODE=%r is not a valid backend (302.AI was removed); "
        "using 'client'.",
        _env("SEPARATION_MODE", "client"),
    )

VALID_SEPARATION_MODES = ("client", "off")


def separation_capabilities() -> dict[str, dict]:
    """
    Which separation backends are usable right now, and why not.

    The UI builds its backend selector from this. The previous design exposed a
    single global `mode` and left the frontend guessing whether it worked, which
    is how "no separation backend is available" ended up being shown while the
    browser backend was perfectly fine.
    """
    browser_model = os.path.join(MODELS_DIR, SEPARATOR_MODEL_FILE)
    browser_ok = os.path.exists(browser_model)

    return {
        "client": {
            "available": browser_ok,
            "reason": None
            if browser_ok
            else f"separator model '{SEPARATOR_MODEL_FILE}' not found in {MODELS_DIR}",
        },
        "off": {"available": True, "reason": None},
    }

# ----- Capability tokens for the client-side compute endpoints -----
# Signs the short-lived tokens that gate the 64 MB model download and the
# stem upload. Set a stable value in .env; otherwise an ephemeral one is
# generated and tokens break on every restart.
SIGNING_SECRET = (os.environ.get("SIGNING_SECRET") or "").strip()
if not SIGNING_SECRET:
    import secrets as _secrets

    SIGNING_SECRET = _secrets.token_urlsafe(32)
    logger.warning(
        "SIGNING_SECRET is not set: generated an ephemeral one. Separator tokens "
        "will be invalidated on restart and will not work across multiple "
        "workers. Set SIGNING_SECRET in backend/.env."
    )
TOKEN_TTL_SECONDS = _env_int("TOKEN_TTL_SECONDS", 1800)
TOKEN_ISSUE_LIMIT = _env_int("TOKEN_ISSUE_LIMIT", 30)
TOKEN_ISSUE_WINDOW = _env_int("TOKEN_ISSUE_WINDOW", 300)
# Set false only for local debugging; the client-compute endpoints are then open.
REQUIRE_SEPARATOR_TOKEN = _env_bool("REQUIRE_SEPARATOR_TOKEN", True)
SEPARATOR_PARAMS = {
    # The ONNX graph fixes dim_f=3072 and dim_t=256. n_fft=6144 follows from
    # UVR's convention dim_f = n_fft/2 (verified empirically against the model:
    # see frontend/scripts/test-mdx-model.mjs), hop_length = n_fft/4.
    "nFft": 6144,
    "dimF": 3072,
    "segmentSize": 256,
    "overlap": _env_float("SEPARATOR_OVERLAP", 0.25),
    "compensate": 1.035,
    "primaryStem": "instrumental",
    "zeroLowBins": 3,
    "normalizationThreshold": 1.0,
}


# =====================================================================
# Export (ffmpeg mux, video stream copied -> no re-encode)
# =====================================================================

# 没有 EXPORT_ENABLED：出片能不能做，取决于 ffmpeg/ffprobe 在不在
# （export_service.is_available()）—— 这是真实约束，而不是一个能让用户点下
# 导出键却得到 "Export is disabled" 的环境开关。
EXPORT_SAMPLE_RATE = _env_int("EXPORT_SAMPLE_RATE", 24000)
EXPORT_TTS_GAIN = _env_float("EXPORT_TTS_GAIN", 1.0)
# Gain applied to the separated background stem (or to the original audio when
# separation was skipped) so it sits under the dubbed voice.
EXPORT_BGM_GAIN = _env_float("EXPORT_BGM_GAIN", 0.9)
EXPORT_ORIGINAL_AUDIO_GAIN = _env_float("EXPORT_ORIGINAL_AUDIO_GAIN", 0.12)
EXPORT_FILENAME = _env("EXPORT_FILENAME", "translated_video.mp4")
# Container used when a styled (ASS) subtitle track is requested. MP4 cannot
# carry ASS styling, so that delivery switches to Matroska.
EXPORT_STYLED_FILENAME = _env("EXPORT_STYLED_FILENAME", "translated_video.mkv")


# =====================================================================
# Subtitles
# =====================================================================
#
# Sizes are PERCENTAGES OF THE VIDEO HEIGHT, never pixels, so one style resolves
# correctly for any resolution and agrees with the in-app preview overlay. See
# app/services/subtitle_service.py.
#
# Nothing here renders text: the server only writes SRT/ASS (plain text) and the
# player does the rendering, so no font has to be installed on the server.
# Burn-in is different — it must rasterise glyphs, which needs libass/freetype
# and a CJK font, and it forces a full re-encode. On a 2 GB CPU-only host that
# is not viable, so burning runs in the BROWSER (Canvas2D + MediaRecorder) where
# the work is free and the OS fonts are already available.

# translated | original | bilingual
SUBTITLE_DEFAULT_TRACK = _env("SUBTITLE_DEFAULT_TRACK", "translated")
# Player-side font name. Only used by players that support styling (MKV/ASS);
# embedded MP4 tracks ignore it.
SUBTITLE_FONT_FAMILY = _env("SUBTITLE_FONT_FAMILY", "Source Han Sans")
SUBTITLE_FONT_SIZE_PERCENT = _env_float("SUBTITLE_FONT_SIZE_PERCENT", 4.5)
SUBTITLE_MARGIN_V_PERCENT = _env_float("SUBTITLE_MARGIN_V_PERCENT", 6.0)
SUBTITLE_MARGIN_H_PERCENT = _env_float("SUBTITLE_MARGIN_H_PERCENT", 5.0)
SUBTITLE_MAX_CHARS_PER_LINE = _env_int("SUBTITLE_MAX_CHARS_PER_LINE", 18)

# How subtitles are delivered in the exported file:
#   "off"    — no subtitle track
#   "soft"   — MP4 with `mov_text` tracks: switchable and toggleable in the
#              player, but unstyled (the format carries no styling at all)
#   "styled" — MKV with a fully styled ASS track. Matroska is required because
#              MP4 cannot carry ASS styling; the VIDEO STREAM IS STILL COPIED,
#              so this costs one remux, not a re-encode.
#   "burn"   — rendered into the picture. Only the BROWSER can do this (see the
#              note above): on the server it is reported unavailable.
#
# "soft" is the default because it is free on every axis: no re-encode, no
# styling to get wrong, and every player understands it.
# 没有 SUBTITLE_EXPORT_ENABLED：字幕导出是每个工程自己的计划（用户可在
# 导出面板里改），默认开启；能力差异（容器是否支持多轨、浏览器烧字幕等）
# 由导出计划的能力位如实上报。
SUBTITLE_EXPORT_FORMAT = _env("SUBTITLE_EXPORT_FORMAT", "soft").lower()
# Comma-separated. Multiple tracks make the file usable for both viewers who
# want the dub and viewers who want the original.
SUBTITLE_EXPORT_TRACKS = [
    t.strip() for t in _env("SUBTITLE_EXPORT_TRACKS", "translated").split(",") if t.strip()
]


# =====================================================================
# File storage / server
# =====================================================================

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

# ---- 上传限制 ----
# 以前上传既不限大小也不查类型：任意客户端可以写满磁盘，或塞非视频文件让
# ffmpeg 去啃。白名单按扩展名判定，拷贝时按字节数实时中止。
UPLOAD_MAX_BYTES = _env_int("UPLOAD_MAX_BYTES", 2 * 1024 * 1024 * 1024)  # 2 GiB
UPLOAD_ALLOWED_EXTENSIONS = {
    e.strip().lower()
    for e in _env(
        "UPLOAD_ALLOWED_EXTENSIONS",
        ".mp4,.m4v,.mov,.mkv,.webm,.avi,.flv,.ts",
    ).split(",")
    if e.strip()
}

CORS_ORIGINS = [o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()]

# Public URL of this server. Required because DashScope must be able to
# download the audio it transcribes / the voice sample it enrolls.
SERVER_URL_BASE = _env("SERVER_URL_BASE", "https://video-voice-translator.idealbroker.cn").rstrip("/")

API_GENERAL_TIMEOUT = _env_int("API_GENERAL_TIMEOUT", 120)
