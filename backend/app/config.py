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

ASR_MODEL = _env("ASR_MODEL", "fun-asr")

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
# Used when the configured model rejects `diarization_enabled`.
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
# TTS (Ali DashScope CosyVoice)
# =====================================================================

TTS_MODEL = _env("TTS_MODEL", "cosyvoice-v3-flash")
TTS_TIMEOUT = _env_int("TTS_TIMEOUT", 60)

# Number of concurrent synthesis calls. This is I/O bound (blocking SDK call
# runs in a thread pool) so it does not increase CPU usage. Raise it only if
# the provider does not rate-limit you.
TTS_CONCURRENCY = _env_int("TTS_CONCURRENCY", 4)
TTS_MAX_RETRIES = _env_int("TTS_MAX_RETRIES", 2)

# Audio format used for the synthesized files and the export timeline.
TTS_SAMPLE_RATE = _env_int("TTS_SAMPLE_RATE", 24000)

# Voice pool for multi-speaker random assignment (only used when voice
# cloning is disabled and no explicit voice is given).
TTS_VOICES = [
    v.strip()
    for v in _env(
        "TTS_VOICES",
        "longanyang,longanhuan,longxiaochun_v3,longcheng_v3,longze_v3,longhua_v3,"
        "longtian_v3,longyan_v3,longshuo_v3,longwan_v3,longanyun_v3,longanwen_v3",
    ).split(",")
    if v.strip()
]
TTS_DEFAULT_VOICE = _env("TTS_DEFAULT_VOICE", "longanyang")


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
