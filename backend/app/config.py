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

LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
LLM_MODEL = _env("LLM_MODEL", "Pro/moonshotai/Kimi-K2.5")
LLM_THINKING_ENABLED = _env_bool("LLM_THINKING_ENABLED", False)
LLM_TIMEOUT = _env_int("LLM_TIMEOUT", 180)
LLM_TEMPERATURE = _env_float("LLM_TEMPERATURE", 0.3)

# Long scripts are translated in chunks so a single response can never be
# truncated mid-JSON. Chunks overlap so the model keeps conversational context.
LLM_CHUNK_MAX_SEGMENTS = _env_int("LLM_CHUNK_MAX_SEGMENTS", 25)
LLM_CHUNK_MAX_CHARS = _env_int("LLM_CHUNK_MAX_CHARS", 4000)
LLM_CHUNK_CONTEXT_SEGMENTS = _env_int("LLM_CHUNK_CONTEXT_SEGMENTS", 3)
LLM_CHUNK_MAX_RETRIES = _env_int("LLM_CHUNK_MAX_RETRIES", 2)

# Extract a glossary (names, jargon, product terms) from the full source script
# before translating, so terminology stays consistent across chunks.
GLOSSARY_AUTO_EXTRACT = _env_bool("GLOSSARY_AUTO_EXTRACT", True)
GLOSSARY_MAX_TERMS = _env_int("GLOSSARY_MAX_TERMS", 40)
# Optional per-video glossary file name inside the video directory.
GLOSSARY_FILE = _env("GLOSSARY_FILE", "glossary.json")

# Rough speaking-rate budgets, used to ask the model to fit each line into the
# time slot it has to fill. Keeps dubbing length roughly aligned with the video
# so the TTS output does not have to be time-stretched on playback.
# Values are characters/sec for CJK and words/sec for latin scripts.
SPEECH_RATE_VALUE = {
    "Chinese": 4.5,
    "Japanese": 5.0,
    "Korean": 4.5,
    "English": 2.8,
    "French": 2.8,
    "German": 2.3,
    "Spanish": 3.2,
}
DEFAULT_SPEECH_RATE_VALUE = 4.0
SPEECH_RATE_UNIT = {
    "Chinese": "Chinese characters",
    "Japanese": "Japanese characters",
    "Korean": "Korean characters",
}
DEFAULT_SPEECH_RATE_UNIT = "words"


def speech_rate_hint(language: str) -> str:
    """Human-readable speaking-rate budget for the given target language."""
    rate = SPEECH_RATE_VALUE.get(language, DEFAULT_SPEECH_RATE_VALUE)
    unit = SPEECH_RATE_UNIT.get(language, DEFAULT_SPEECH_RATE_UNIT)
    return f"about {rate} {unit} per second"


def speech_rate_value(language: str) -> float:
    """Numeric speaking rate (chars or words per second)."""
    return SPEECH_RATE_VALUE.get(language, DEFAULT_SPEECH_RATE_VALUE)


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
ASR_DIARIZATION_ENABLED = _env_bool("ASR_DIARIZATION_ENABLED", True)
# Used when the configured model rejects `diarization_enabled`. Deliberately a
# different model family so a provider-side outage of one line still works.
ASR_FALLBACK_MODEL = _env("ASR_FALLBACK_MODEL", "fun-asr")

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

# Number of concurrent synthesis calls. This is I/O bound (blocking SDK call
# runs in a thread pool) so it does not increase CPU usage. Raise it only if
# the provider does not rate-limit you.
TTS_CONCURRENCY = _env_int("TTS_CONCURRENCY", 4)
TTS_MAX_RETRIES = _env_int("TTS_MAX_RETRIES", 2)

# Audio format used for the synthesized files and the export timeline.
# The SDK binds the sample rate to its `format` enum (default mp3 22.05 kHz);
# export re-samples with ffmpeg, so this only sizes the mixing timeline.
TTS_SAMPLE_RATE = _env_int("TTS_SAMPLE_RATE", 24000)

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
    # KNOWN GAP: the four multilingual *system* voices are documented for
    # Japanese/Korean/French/German/Portuguese/Italian/Vietnamese/Indonesian —
    # Spanish is NOT among them, and the UI offers Spanish. Spanish therefore
    # falls back to the multilingual pool, which may mispronounce (the docs
    # warn "可能发音错误或语音不自然" for unsupported pairs; it is a quality
    # issue, not an error). Voice *cloning* does cover Spanish, so for Spanish
    # output prefer enabling voice cloning, or drop Spanish from the UI list.
    "Spanish": TTS_MULTILINGUAL_VOICES,
}

# Fallback pool / manual override. Defaults to the multilingual set because it
# is the only one that is safe for an unknown target language.
TTS_VOICES = [
    v.strip()
    for v in _env("TTS_VOICES", ",".join(TTS_MULTILINGUAL_VOICES)).split(",")
    if v.strip()
]
TTS_DEFAULT_VOICE = _env("TTS_DEFAULT_VOICE", "longanhuan_v3.1")


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
# Three modes:
#   "client" — the browser runs the MDX-Net ONNX model (WebGPU/WASM) and
#              uploads the resulting stems. No server CPU/RAM cost, no API
#              cost. This is the default because the server has no headroom.
#   "server" — the server runs `audio-separator` (PyTorch). Only viable on a
#              machine with real headroom; NOT on a 2 GB / single-core host:
#                pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
#                pip install "audio-separator[cpu]>=0.24.0"
#   "off"    — no separation at all.
SEPARATION_MODE = _env("SEPARATION_MODE", "client").lower()

# Server-side (PyTorch) separation backend. Disabled by default: importing
# torch costs hundreds of MB of RSS and will make a small host swap.
ENABLE_BGM_SEPARATION_DEFAULT = _env_bool("ENABLE_BGM_SEPARATION_DEFAULT", False)
PRELOAD_SEPARATION = _env_bool("PRELOAD_SEPARATION", False)
SEPARATION_MODEL_NAME = _env("SEPARATION_MODEL_NAME", "2_HP-UVR.pth")

# Client-side (browser) separation: the MDX-Net model served to the browser.
# Parameters are mirrored to the client via GET /api/models/separator so the
# JavaScript never has to hardcode them.
MODELS_DIR = os.path.join(BASE_DIR, "models")
SEPARATOR_MODEL_FILE = _env("SEPARATOR_MODEL_FILE", "UVR-MDX-NET-Inst_HQ_3.onnx")

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

EXPORT_ENABLED = _env_bool("EXPORT_ENABLED", True)
EXPORT_SAMPLE_RATE = _env_int("EXPORT_SAMPLE_RATE", 24000)
EXPORT_TTS_GAIN = _env_float("EXPORT_TTS_GAIN", 1.0)
# Gain applied to the separated background stem (or to the original audio when
# separation was skipped) so it sits under the dubbed voice.
EXPORT_BGM_GAIN = _env_float("EXPORT_BGM_GAIN", 0.9)
EXPORT_ORIGINAL_AUDIO_GAIN = _env_float("EXPORT_ORIGINAL_AUDIO_GAIN", 0.12)
EXPORT_FILENAME = _env("EXPORT_FILENAME", "translated_video.mp4")


# =====================================================================
# File storage / server
# =====================================================================

DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)

CORS_ORIGINS = [o.strip() for o in _env("CORS_ORIGINS", "*").split(",") if o.strip()]

# Public URL of this server. Required because DashScope must be able to
# download the audio it transcribes / the voice sample it enrolls.
SERVER_URL_BASE = _env("SERVER_URL_BASE", "http://119.45.51.201").rstrip("/")

API_GENERAL_TIMEOUT = _env_int("API_GENERAL_TIMEOUT", 120)
