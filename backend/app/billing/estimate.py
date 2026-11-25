"""
初始化费用的预估：按时长 + 设置估一个**略偏高**的积分数，用作开跑前的余额门槛。

为什么要估而不是固定门槛：一个 30 秒的短片和一小时的讲座差两个数量级，固定门槛要么
拦住短片用户、要么放行长片把余额花穿。

成本模型（按真实账本**一次干净初始化**校准：34 秒 / 9 句 / 3 克隆 ≈ 23 积分，
见 backend/data/44767c2a…/usage.json 里去掉 chat / pick_samples / 重跑之后的部分）：

  · ASR   按 token，≈ 时长成正比（34 秒 ≈ 371 入 / 120 出）
  · omni  审听按句，每句 ≈ 1000 入 / 300 出（音频 token 占大头）
  · LLM   翻译按**分块**，每块 ≈ 1470 入 / 1400 出（输出含推理内容，和输入一样多）；
          块数 = 句数 / 每块句数 + 一次术语表抽取
  · clone 按说话人数（每音色一次），保守估 3
  · TTS   按句，每句 ≈ 15 入 + 音频 token（约 50/秒）
  · 句数 ≈ 时长 × 16 句/分钟（该工程 34 秒 9 句）

上一版把"这个工程总共花了多少（含多轮 chat、克隆选材、重跑）"当成了"一次初始化要多少"，
估高了 2 倍。这里只算初始化本身。
"""
from __future__ import annotations

from app import config
from app.billing import pricing
from app.billing.pricing import MICRO

SENTENCES_PER_MINUTE = 16            # 34 秒 9 句 ≈ 16 句/分
LLM_SEGMENTS_PER_CHUNK = 4           # 9 句 → 3 块
SPEAKERS_ASSUME = 3

ASR_INPUT_TOKENS_PER_SECOND = 11
ASR_OUTPUT_TOKENS_PER_SECOND = 3.5

OMNI_INPUT_TOKENS_PER_SENTENCE = 1_000
OMNI_OUTPUT_TOKENS_PER_SENTENCE = 300

LLM_CHUNK_INPUT_TOKENS = 1_500
LLM_CHUNK_OUTPUT_TOKENS = 1_400
LLM_GLOSSARY_INPUT_TOKENS = 350
LLM_GLOSSARY_OUTPUT_TOKENS = 450

TTS_INPUT_TOKENS_PER_SENTENCE = 20
TTS_OUTPUT_TOKENS_PER_SECOND = 55


def _points(step: str, model: str, usage: dict) -> int:
    try:
        return pricing.price_micro(step, model, usage, markups=config.BILLING_MARKUP)[1]
    except pricing.NoCost:
        return 0


def estimate_init_points(duration_seconds: float, *, dubbing_mode: str = "remote",
                         mm_enhance: bool = True, voice_clone: bool = True,
                         speakers: int = SPEAKERS_ASSUME) -> dict:
    """
    估一次完整初始化要花多少积分。返回 {"points": 积分数, "points_micro": ..., "breakdown": {...}}。

    `dubbing_mode`: remote（远端 TTS + 可选克隆）/ browser（本地，免费）/ off（只字幕，不配音不克隆）
    """
    dur = max(0.0, float(duration_seconds or 0))
    sentences = max(1, int(dur / 60 * SENTENCES_PER_MINUTE)) if dur else 1
    parts: dict[str, int] = {}

    parts["asr"] = _points("asr", config.ASR_MODEL, {
        "input_tokens": int(dur * ASR_INPUT_TOKENS_PER_SECOND),
        "output_tokens": int(dur * ASR_OUTPUT_TOKENS_PER_SECOND),
    })

    if mm_enhance:
        parts["omni"] = _points("omni", config.OMNI_MODEL, {
            "input_tokens": sentences * OMNI_INPUT_TOKENS_PER_SENTENCE,
            "output_tokens": sentences * OMNI_OUTPUT_TOKENS_PER_SENTENCE,
        })

    chunks = max(1, -(-sentences // LLM_SEGMENTS_PER_CHUNK))          # ceil
    parts["llm"] = _points("llm", config.LLM_MODEL, {
        "input_tokens": LLM_GLOSSARY_INPUT_TOKENS + chunks * LLM_CHUNK_INPUT_TOKENS,
        "output_tokens": LLM_GLOSSARY_OUTPUT_TOKENS + chunks * LLM_CHUNK_OUTPUT_TOKENS,
    })

    if dubbing_mode == "remote":
        if voice_clone:
            parts["clone"] = _points("clone", pricing.ENROLLMENT_MODEL, {}) * max(1, speakers)
        parts["tts"] = _points("tts", config.TTS_MODEL, {
            "input_tokens": sentences * TTS_INPUT_TOKENS_PER_SENTENCE,
            "output_tokens": int(dur * TTS_OUTPUT_TOKENS_PER_SECOND),
        })

    total = sum(parts.values())
    return {
        "points_micro": total,
        "points": pricing.as_points(total),
        "duration_seconds": round(dur, 1),
        "sentences": sentences,
        "breakdown": {k: pricing.as_points(v) for k, v in parts.items() if v},
    }
