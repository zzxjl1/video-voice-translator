"""
qwen-omni 多模态调用 —— 两个"听音频干活"的功能都从这里走。

1. enhance_transcript: 听原声，对 ASR 转写做二次处理 —— 审核纠错（同音字、
   错词、标点）并标注语气标签。输出随工程落盘，DeepSeek 拿到的是【ASR 原文
   + omni 审听版】两份，由它综合出最终带标签的译文。

2. pick_samples: 声音复刻的参考素材挑选。通过 tool call 让模型从候选段里
   选，每一轮选择都被校验（id 归属 / 时长 / 重叠），不合格就把原因喂回去
   重选；超过轮数上限返回 None，由调用方回退到规则选材。

所有失败都是"降级"而不是"报错"：这俩是增强功能，挂了就该退回无标签 /
规则选材，绝不能影响主流程。
"""

import json
import logging

from openai import AsyncOpenAI

from app import config
from app.services import usage_service

logger = logging.getLogger(__name__)

# 超时是量过之后定的：这里传的是音频 URL，由模型服务端去拉。URL 不可达时它
# 会一直重试下载，而 SDK 默认重试 2 次、每次 300s —— 单次调用最坏能把管线拖
# 住 15 分钟，界面上看起来就是"卡在 ASR 之后不动了"。
#   - timeout=300：实测一次真实调用（9 行、42 秒音频）耗时 120.6s，所以 180s
#     太紧（慢一点就误判失败），而 300s 留了一倍余量。这是【单次】上限，不乘
#     重试次数。
#   - max_retries=0：管线对 omni 的失败本来就是降级继续（用原始 ASR 文本），
#     重试没有价值，只会把"失败"拖成"卡住"。
# 更长的视频会超出这个预算并降级 —— 那时的正解是分块送（未做）。
_client = AsyncOpenAI(
    api_key=config.DASHSCOPE_API_KEY,
    base_url=config.OMNI_BASE_URL,
    timeout=300,
    max_retries=0,
)


def _audio_part(url: str) -> dict:
    return {"type": "input_audio", "input_audio": {"data": url, "format": "wav"}}


async def enhance_transcript(
    video_id: str,
    items: list[dict],
    audio_url: str,
) -> dict[str, str]:
    """
    听原声，对 ASR 转写做二次处理：纠正识别错误 + 标注语气标签。

    items: [{"id": ..., "text": <ASR 原文>}]. 返回 {id: 增强后的文本} ——
    【每一条都要返回】（纠错是身份性的：没纠错也要原样返回），调用方据此
    覆盖翻译输入。失败返回 {}（调用方回退原始 ASR 文本）。
    """
    if not items or not audio_url:
        return {}

    tag_list = " ".join(config.EMOTION_CONTROL_TAGS)
    lines = "\n".join(f'{item["id"]}: {item["text"]}' for item in items)
    system = (
        "You are a transcription reviewer and dubbing director. You HEAR the "
        "original video audio and read the ASR transcript of the same video.\n\n"
        "SEGMENTATION IS AUTHORITATIVE. The line boundaries below come from "
        "forced alignment on the ASR result. Keep them EXACTLY as given: do not "
        "merge lines, split lines, re-order them, or move text across lines. "
        "Your own sense of timing is far less accurate than these boundaries — "
        "work strictly within each given line.\n\n"
        "For EVERY line do two things:\n"
        "1. PROOFREAD the transcript against what you actually hear: fix "
        "mis-recognized words (homophones are common), missing or wrong "
        "punctuation, and duplicated or dropped words. Keep the ORIGINAL "
        "language — never translate. Do not change the meaning.\n"
        "2. Annotate delivery: you MAY place a control tag at the start of the "
        f"line ({tag_list}) and rich sound tags inline where the sound happens "
        f"({' '.join(config.EMOTION_RICH_TAGS)}). Most lines are neutral — use "
        "tags only when the delivery clearly warrants it.\n\n"
        "Reply with a JSON object: {\"lines\": [{\"id\": \"...\", \"text\": "
        "\"...\"}]} — one entry per input id, in the original language, ids "
        "unchanged. Never add explanations."
    )

    usage_total: dict = {}
    by_id: dict | None = None
    for attempt in (1, 2):  # 一次失败重试一次，再失败就放弃
        try:
            r = await _client.chat.completions.create(
                model=config.OMNI_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{system}\n\nLines:\n{lines}"},
                        _audio_part(audio_url),
                    ],
                }],
                response_format={"type": "json_object"},
            )
            usage = r.usage.model_dump() if r.usage else {}
            for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens"):
                if usage.get(key):
                    usage_total[key] = usage_total.get(key, 0) + usage[key]
            data = json.loads(r.choices[0].message.content or "{}")
            lines_out = data.get("lines") or []
            by_id = {
                str(entry.get("id")): str(entry.get("text") or "")
                for entry in lines_out
                if isinstance(entry, dict) and entry.get("id") is not None
            }
            if set(by_id.keys()) != {str(item["id"]) for item in items}:
                raise ValueError("returned ids do not match the input")
            break
        except Exception as e:
            logger.warning(f"[MmEnhance] attempt {attempt} failed: {e}")
            by_id = None
    usage_service.record(video_id=video_id, step="omni", model=config.OMNI_MODEL,
                         detail="mm_enhance", usage=usage_total)

    if not by_id:
        return {}

    enhanced: dict[str, str] = {}
    for item in items:
        # 唯一的硬校验：非空。纠错改多少是模型基于听觉的判断，不设幅度上限
        # —— 这类死规则过滤效果差，误伤真纠正的代价更高。
        new_text = (by_id[str(item["id"])] or "").strip()
        enhanced[item["id"]] = new_text or item["text"]
    changed = sum(1 for item in items if enhanced[item["id"]] != item["text"])
    logger.info(f"[MmEnhance] {video_id}: {changed}/{len(items)} line(s) adjusted by review")
    return enhanced


async def pick_samples(
    video_id: str,
    speaker_id: str,
    candidates: list[dict],
    audio_url: str,
) -> list[str] | None:
    """
    让 omni 听音频，通过 tool call 挑选克隆参考段。

    candidates: [{"id", "start", "end", "duration", "text"}]（同一说话人）。
    返回选中的 id 列表（已通过全部校验），或 None（超轮数/失败 → 调用方回退
    到规则选材）。
    """
    if not candidates or not audio_url:
        return None

    by_id = {str(c["id"]): c for c in candidates}
    listing = "\n".join(
        f'id={c["id"]}  [{c["start"]:.2f}-{c["end"]:.2f}]s  {c["duration"]:.1f}s  '
        f'"{c["text"][:60]}"'
        for c in candidates
    )
    system = (
        "You are a voice-cloning casting director. You hear the ORIGINAL audio "
        "and must pick the reference segments for cloning ONE speaker's voice.\n\n"
        "HARD RULES for every pick:\n"
        "- The span must contain ONLY that speaker's clean speech. Skip any span "
        "where another voice talks over them — overlapping speech poisons the "
        "timbre.\n"
        "- Skip extreme emotion: shouting, crying, laughing fits, panicked or "
        "whispered lines. Timbre comes from the reference; emotion is added "
        "later by tags. Pick neutral, normal-paced delivery.\n"
        "- Skip noise and music-heavy spans.\n"
        "- Together the picks should total 10-30 seconds and must not overlap "
        "each other in time.\n"
        "- Prefer longer, complete sentences over fragments.\n\n"
        "You MUST answer by calling the select_samples tool with the chosen ids "
        "and a one-line reason for each. If a previous attempt was rejected, fix "
        "the reported problem and call the tool again."
    )
    user_text = f"Speaker: {speaker_id}\nCandidate segments:\n{listing}"
    tools = [{
        "type": "function",
        "function": {
            "name": "select_samples",
            "description": "Select the reference segments for voice cloning.",
            "parameters": {
                "type": "object",
                "properties": {
                    "segments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["id", "reason"],
                        },
                    },
                },
                "required": ["segments"],
            },
        },
    }]

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            _audio_part(audio_url),
        ]},
    ]

    usage_total: dict = {}
    for turn in range(1, config.CLONE_OMNI_MAX_TURNS + 1):
        try:
            r = await _client.chat.completions.create(
                model=config.OMNI_MODEL, messages=messages, tools=tools,
                tool_choice="auto",
            )
            usage = r.usage.model_dump() if r.usage else {}
            for key in ("input_tokens", "prompt_tokens", "output_tokens", "completion_tokens"):
                if usage.get(key):
                    usage_total[key] = usage_total.get(key, 0) + usage[key]
        except Exception as e:
            logger.warning(f"[SamplePick] {speaker_id} turn {turn} API error: {e}")
            return None

        msg = r.choices[0].message
        calls = getattr(msg, "tool_calls", None) or []
        if not calls:
            logger.warning(f"[SamplePick] {speaker_id} turn {turn}: no tool call, nagging")
            messages.append({"role": "assistant", "content": msg.content or ""})
            messages.append({"role": "user",
                             "content": "You MUST call the select_samples tool now."})
            continue

        picked_ids: list[str] = []
        reasons: list[str] = []
        for call in calls:
            if call.function.name != "select_samples":
                continue
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                continue
            for seg in args.get("segments", []):
                if isinstance(seg, dict) and seg.get("id") is not None:
                    picked_ids.append(str(seg["id"]))
                    reasons.append(f"{seg['id']}: {seg.get('reason', '')}")

        # ---- 校验：任何一条不过就把原因喂回去重选 ----
        unknown = [i for i in picked_ids if i not in by_id]
        picked = [by_id[i] for i in dict.fromkeys(picked_ids) if i in by_id]
        picked.sort(key=lambda s: s["start"])
        problems: list[str] = []
        if unknown:
            problems.append(f"unknown ids: {unknown}")
        if not picked:
            problems.append("no valid segments selected")
        else:
            overlaps = [
                (a["id"], b["id"]) for a, b in zip(picked, picked[1:])
                if a["end"] - b["start"] > 0.05
            ]
            if overlaps:
                problems.append(f"selected spans overlap each other: {overlaps}")
            total = sum(c["duration"] for c in picked)
            if total > 30.0:
                problems.append(f"total {total:.1f}s exceeds 30s — drop some picks")
            elif total < 5.0:
                problems.append(f"total {total:.1f}s is below 5s — pick more or longer segments")

        if not problems:
            usage_service.record(video_id=video_id, step="omni", model=config.OMNI_MODEL,
                                 detail=f"pick_samples({speaker_id})", usage=usage_total)
            logger.info(
                f"[SamplePick] {speaker_id}: accepted after {turn} turn(s): "
                f"{[c['id'] for c in picked]} ({'; '.join(reasons)[:200]})"
            )
            return [c["id"] for c in picked]

        reason = "; ".join(problems)
        logger.warning(f"[SamplePick] {speaker_id} turn {turn} rejected: {reason}")
        # 把这轮的工具调用与"失败原因"作为 tool result 喂回去，进入下一轮
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in calls
            ],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": calls[0].id,
            "content": f"REJECTED: {reason}. Fix the problem and call select_samples again.",
        })

    logger.warning(
        f"[SamplePick] {speaker_id}: gave up after {config.CLONE_OMNI_MAX_TURNS} "
        "turns — caller will fall back to rule-based selection"
    )
    return None
