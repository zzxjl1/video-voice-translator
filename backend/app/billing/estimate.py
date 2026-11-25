"""
初始化费用的预估：按时长 + 设置估一个**保守偏高**的积分数，用作开跑前的余额门槛。

为什么要估而不是固定门槛：一个 30 秒的短片和一小时的讲座差两个数量级，固定门槛要么
拦住短片用户、要么放行长片把余额花穿。

成本模型（按真实账本校准，34 秒视频实际 ≈ 40 积分，估算 ≈ 45）：
  · ASR / TTS / omni 按用量（token）计费，近似与时长成正比
  · LLM 翻译 / omni 审听按**句数**计费，句数 ≈ 时长 × 语速；语速取偏快的中文口播
  · 声音克隆按说话人数（每音色一次），用保守的说话人数估计
估算是"上界"而不是精确值：宁可让用户多备一点积分，也不要跑一半发现不够。
真实扣费仍按每次调用的实际用量（billing.service.charge）。
"""
from __future__ import annotations

from app import config
from app.billing import pricing
from app.billing.pricing import MICRO

# ---- 系数来源：真实账本（34 秒 / 9 句 / 3 个克隆音色，backend/data/44767c2a…/usage.json）----
# 校准目标：估算 ≥ 实际。实际 40.4 积分；以下系数让 34 秒估到 ~45（高 ~10%）。
#
# 句数：该工程 34 秒 9 句 ≈ 16 句/分钟；取 18 略偏高。
SENTENCES_PER_MINUTE = 18
# 说话人数：按 3 估（短片段 1–2 人，访谈类更多；克隆按次计费，宁可高估）
SPEAKERS_ASSUME = 3
# LLM：7 次调用 / 9 句，输入 6446 输出 8187 → 每句 ≈ 715 入 / 910 出（输出含推理内容，
# 比输入还多——这是 deepseek 推理模型的特征，不是翻译文本本身长）。再乘分块/术语表的
# 固定开销，取 900 / 1100。
LLM_INPUT_TOKENS_PER_SENTENCE = 900
LLM_OUTPUT_TOKENS_PER_SENTENCE = 1_100
# omni 审听：17 次调用里大部分是审听（mm_enhance），输入 74500 → 每句 ≈ 8.3k
# （音频 token 占大头，与句长成正比）；输出 3199 → 每句 ≈ 355。
OMNI_INPUT_TOKENS_PER_SENTENCE = 8_500
OMNI_OUTPUT_TOKENS_PER_SENTENCE = 400
# ASR：34 秒，输入 741 输出 239（两次调用含 fallback）
ASR_INPUT_TOKENS_PER_SECOND = 22
ASR_OUTPUT_TOKENS_PER_SECOND = 7
# TTS：47 次调用 / 9 句 ≈ 5 次/句（时长拟合重合成 + 克隆试听）；每次输入 ≈ 15 token，
# 输出 2121 token / 34 秒 ≈ 62 token/秒。
TTS_INPUT_TOKENS_PER_SENTENCE = 75
TTS_OUTPUT_TOKENS_PER_SECOND = 62


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
    minutes = dur / 60
    sentences = max(1, int(minutes * SENTENCES_PER_MINUTE))
    marks = config.BILLING_MARKUP
    parts: dict[str, int] = {}

    # ASR：转写整段音频
    parts["asr"] = _points("asr", config.ASR_MODEL, {
        "input_tokens": int(dur * ASR_INPUT_TOKENS_PER_SECOND),
        "output_tokens": int(dur * ASR_OUTPUT_TOKENS_PER_SECOND),
    })

    # omni 审听：按句听原声（开着才花）
    if mm_enhance:
        parts["omni"] = _points("omni", config.OMNI_MODEL, {
            "input_tokens": sentences * OMNI_INPUT_TOKENS_PER_SENTENCE,
            "output_tokens": sentences * OMNI_OUTPUT_TOKENS_PER_SENTENCE,
        })

    # 翻译：LLM 按句
    parts["llm"] = _points("llm", config.LLM_MODEL, {
        "input_tokens": sentences * LLM_INPUT_TOKENS_PER_SENTENCE,
        "output_tokens": sentences * LLM_OUTPUT_TOKENS_PER_SENTENCE,
    })

    # 配音与克隆：只在本工程要配音时有
    if dubbing_mode == "remote":
        if voice_clone:
            parts["clone"] = _points("clone", pricing.ENROLLMENT_MODEL, {}) * max(1, speakers)
        parts["tts"] = _points("tts", config.TTS_MODEL, {
            "input_tokens": sentences * TTS_INPUT_TOKENS_PER_SENTENCE,
            "output_tokens": int(dur * TTS_OUTPUT_TOKENS_PER_SECOND),
        })
    # browser：本地合成，免费；off：不配音

    total = sum(parts.values())
    return {
        "points_micro": total,
        "points": pricing.as_points(total),
        "duration_seconds": round(dur, 1),
        "sentences": sentences,
        "breakdown": {k: pricing.as_points(v) for k, v in parts.items() if v},
    }


def format_points(points: float) -> str:
    return f"{points:.0f}" if points >= 10 else f"{points:.1f}".rstrip("0").rstrip(".")
