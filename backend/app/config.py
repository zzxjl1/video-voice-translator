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
LLM_THINKING_ENABLED = False

# ----- Ali DashScope TTS (CosyVoice3) -----
TTS_MODEL = "cosyvoice-v3-flash"
# Voice pool for multi-speaker random assignment
# Each speaker gets a unique voice from this list
TTS_VOICES = [
    "longanyang",      # 阳光大男孩
    "longanhuan",      # 欢脱元气女
    "longxiaochun_v3", # 知性积极女
    "longcheng_v3",    # 智慧青年男
    "longze_v3",       # 温暖元气男
    "longhua_v3",      # 元气甜美女
    "longtian_v3",     # 磁性理智男
    "longyan_v3",      # 温暖春风女
    "longshuo_v3",     # 博才干练男
    "longwan_v3",      # 细腻柔声女
    "longanyun_v3",    # 居家暖男
    "longanwen_v3",    # 优雅知性女
]
TTS_DEFAULT_VOICE = "longanyang"

# ----- File storage -----
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")

# Ensure directory exists
os.makedirs(DATA_DIR, exist_ok=True)

# ----- Server -----
CORS_ORIGINS = ["*"]
# Public URL of the server (needed for Ali ASR callbacks/downloads)
SERVER_URL_BASE = "http://119.45.51.201"

# ----- Timeouts -----
API_GENERAL_TIMEOUT = 120       # General timeout for HTTP requests (seconds)
ASR_MAX_WAIT = 600        # Maximum time to poll for ASR success (seconds)
LLM_TIMEOUT = 180      # Timeout for LLM translation calls (seconds)
TTS_TIMEOUT = 60        # Timeout for TTS synthesis calls (seconds)
