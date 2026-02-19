"""
Application configuration.
All API keys and settings are managed here.
"""
import os

# ----- Ali DashScope ASR -----
DASHSCOPE_API_KEY = "sk-22b943ff3e5c499abbfbfb33a7cf4451"

# ----- SiliconFlow LLM (via OpenAI SDK) -----
SILICONFLOW_API_KEY = "sk-lxeewliusahjaucqxkmnvgswytrvtwonommtvyeywuzeuibz"
SILICONFLOW_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_MODEL = "Pro/moonshotai/Kimi-K2.5"

# ----- OpenAI TTS -----
# Using SiliconFlow's TTS endpoint via OpenAI SDK
TTS_API_KEY = SILICONFLOW_API_KEY
TTS_BASE_URL = SILICONFLOW_BASE_URL
TTS_MODEL = "FunAudioLLM/CosyVoice2-0.5B"
TTS_VOICE = "alloy"

# ----- File storage -----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
AUDIO_DIR = os.path.join(BASE_DIR, "audio_cache")

# Ensure directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

# ----- Server -----
CORS_ORIGINS = ["*"]
# Public URL of the server (needed for Ali ASR callbacks/downloads)
SERVER_URL_BASE = "http://video-voice-translator.idealbroker.cn"

# ----- Timeouts -----
API_GENERAL_TIMEOUT = 60        # General timeout for HTTP requests (seconds)
ASR_MAX_WAIT = 600        # Maximum time to poll for ASR success (seconds)
LLM_TIMEOUT = 120      # Timeout for LLM translation calls (seconds)
TTS_TIMEOUT = 60        # Timeout for TTS synthesis calls (seconds)
