"""与具体工程无关的元信息：ASR 能听哪些语言、TTS 每种语言有哪些音色。落地页在上传之前就要用。"""
from typing import Optional

from fastapi import APIRouter

from app import config

router = APIRouter(tags=["meta"])


@router.get("/asr/languages")
async def asr_languages():
    """转写模型能听的语言和中文方言。回答"这个视频我能不能用"，而不是偏好。"""
    return {"languages": list(config.ASR_SOURCE_LANGUAGES), "dialects": list(config.ASR_CHINESE_DIALECTS)}


@router.get("/tts/voices")
async def voices_for_language(language: Optional[str] = None):
    """
    哪些内置音色能说 `language`。`needs_voice_cloning` = 没有任何内置音色说这种语言：
    此时列表为空而不是塞一个"接近的" —— 兜底池能出声但读音不对，文档把这种结果当质量
    问题而不是错误，没有任何地方会报告它。
    """
    supported = config.system_voices_support(language)
    return {
        "language": language,
        "voices": [{"id": v, "label": config.voice_label(v)} for v in config.voices_for_language(language)]
        if supported else [],
        "needs_voice_cloning": not supported,
    }
