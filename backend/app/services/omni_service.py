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

# 超时与重试策略（都量过）：
#   - timeout=300：这是【单次】上限，不乘重试次数。正常情况下关掉思考后一次
#     调用是 1-2 秒级（见下），300s 是给"特别长的音频 + 服务端排队"留的余量。
#   - max_retries=0：管线对 omni 的失败本来就是降级继续（用原始 ASR 文本），
#     重试没有价值，只会把"失败"拖成"卡住"。
#
# ⚠️ 【思考模式必须关掉】—— 这是这条链路上最大的性能陷阱，实测数据：
#     同一段音频、同一个 prompt、同一个模型：
#       默认（思考开）：112.7s，output 12268 token，思维链 49075 字符
#       关掉思考      ：  1.2s，output    67 token，思维链 0
#     94 倍差距。任务是"对照音频校对转写并标注语气"，输出只有几百字符，模型却
#     先生成了上万 token 的推理过程 —— 用户看到的就是"卡在 ASR 之后两分钟"。
#     注意 `qwen3.8-omni-flash` 默认开启思考，不显式关闭就会付这个代价。
_THINKING_OFF = {"enable_thinking": False}
_client = AsyncOpenAI(
    api_key=config.DASHSCOPE_API_KEY,
    base_url=config.OMNI_BASE_URL,
    timeout=300,
    max_retries=0,
)


def _audio_part(url: str) -> dict:
    return {"type": "input_audio", "input_audio": {"data": url, "format": "wav"}}


async def _chat(video_id: str, detail: str, **kwargs):
    """
    【所有】 omni 调用的唯一出口：调用与记账在同一个地方完成。

    记账为什么不写在每个 return 之前：那是"成功分支记一次、失败分支各记一次"，
    两处漏一处就少一笔账 —— 实测漏掉的正是最贵的多轮重试。既然"调用发生过就必须
    记账"是个不变量，它就该和调用绑在一起，而不是让每个退出路径自觉。

    返回原始响应；调用抛错时不记账（没有响应 = 没有可计费的用量）。
    """
    r = await _client.chat.completions.create(**kwargs)
    usage = r.usage.model_dump() if r.usage else {}
    if usage:
        usage_service.record(
            video_id=video_id, step="omni", model=config.OMNI_MODEL,
            detail=detail, usage=usage,
        )
    return r


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
        "language — never translate. Do not change the meaning. "
        "Fix anything that is clearly a recognition slip even when it looks "
        "plausible as written: near-homophones that make no sense in context "
        "(e.g. 字体一致 heard as 字体移植, 'at six' heard as 'at sex'), a "
        "name that contradicts the rest of the line, a word that breaks the "
        "sentence. Use the audio as the arbiter — you can hear what was said; "
        "the transcript cannot.\n"
        "2. Annotate delivery: you MAY place a control tag at the start of the "
        f"line ({tag_list}) and rich sound tags inline where the sound happens "
        f"({' '.join(config.EMOTION_RICH_TAGS)}). Tag every line whose delivery "
        "is not plainly neutral; when a tag plausibly fits, tag it. Leave a line "
        "untagged only when the delivery really is flat and neutral.\n"
        "TAG FORMAT (the text is fed straight into a speech synthesizer, so "
        "this is not cosmetic):\n"
        "- Use ONLY the tags listed above, spelled EXACTLY as shown, wrapped in "
        "HALF-WIDTH square brackets like [excited]. Never use full-width "
        "brackets (【excited】), parentheses, or any other decoration.\n"
        "- Never invent, translate or paraphrase a tag name; never write a tag "
        "in Chinese (「大笑」/【大笑】 are all invalid).\n"
        "- Order: at most ONE control tag, at the very START of the line "
        "(before any word). Rich sound tags go inline, exactly where the sound "
        f"happens, at most {config.EMOTION_MAX_RICH_PER_LINE} per line.\n"
        "- Every bracket-marked token you emit must be one of the tags above; "
        "if you cannot pick a fitting tag, emit no tag at all.\n\n"
        "Reply with a JSON object: {\"lines\": [{\"id\": \"...\", \"text\": "
        "\"...\"}]} — one entry per input id, in the original language, ids "
        "unchanged. Never add explanations."
    )

    by_id: dict | None = None
    for attempt in (1, 2):  # 一次失败重试一次，再失败就放弃
        try:
            # 每次尝试都走 _chat：重试也是真实调用，也各自记一笔账（旧的合并记法
            # 只留一行，看起来像只调用过一次）。
            r = await _chat(
                video_id,
                "mm_enhance",
                model=config.OMNI_MODEL,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{system}\n\nLines:\n{lines}"},
                        _audio_part(audio_url),
                    ],
                }],
                response_format={"type": "json_object"},
                # 关掉思考：这一段的输出是"改几个字 + 加标签"，不需要推理过程，
                # 开着会让每次调用多花两分钟（见文件顶部实测）。
                extra_body=_THINKING_OFF,
            )
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


# 校验下限/上限：picks 的总时长必须落在 [下限, 上限] 之间，否则把原因喂回去
# 重选。这两个数【必须】与下面校验块一致 —— 前置可行性检查靠它们判断"omni 有
# 没有可能选出一个合格答案"。
_PICK_MIN_TOTAL_S = 5.0
_PICK_MAX_TOTAL_S = 30.0


async def pick_samples(
    video_id: str,
    speaker_id: str,
    candidates: list[dict],
    audio_url: str,
) -> tuple[list[str] | None, str]:
    """
    让 omni 听音频，通过 tool call 挑选克隆参考段。

    candidates: [{"id", "start", "end", "duration", "text"}]（同一说话人）。

    返回 `(ids, reason)`：
      * 成功 → `(选中的 id 列表, "")`
      * 失败 → `(None, 人话原因)`

    为什么带 reason：以前所有失败都返回一个 None，调用方只能笼统报"omni 没有
    挑出任何段"。而实际上"候选为空""候选总共才 2.4 秒、达不到 5 秒下限""API
    调用失败""模型连轮数给不出可用选择"这四件事的排查方向完全不同 —— 用户看到
    同一句话，我们也看不出是哪一件。
    """
    if not candidates:
        return None, "该说话人在这个工程里没有可用的候选段"
    if not audio_url:
        return None, "音频地址为空，omni 无法听"

    # 下限取 min(建议下限, 实际可用素材)：约束必须【可满足】。
    #
    # 实测过发一个不可满足的约束会怎样（说话人只有一段 2.4s，而下限写死 5s）：
    # 模型第 1 轮正确选择了那一段 → 被"below 5s"驳回 → 第 2 轮它直接说明
    # "the provided candidate list only contains one segment" → 之后每轮都重复，
    # 5 轮全废、白花 5 次调用，最后调用方只能报一句"没挑出任何段"。
    #
    # 正确的做法不是绕过模型，而是把约束放到它做得到的范围，并告诉它原因和真实
    # 可用量 —— 素材少的时候，"从仅有素材里挑最好的"仍然是模型比死规则更会做的判断。
    available = sum(float(c.get("duration") or 0) for c in candidates)
    min_total = min(_PICK_MIN_TOTAL_S, available)
    material_note = (
        ""
        if available >= _PICK_MIN_TOTAL_S
        else (
            f"\n\nMATERIAL LIMIT: this speaker has only {available:.1f}s of usable "
            f"speech in the whole video, so the usual {_PICK_MIN_TOTAL_S:.0f}s floor "
            "cannot be reached. That is expected — pick the best material that "
            "actually exists (never invent ids), and do not keep trying to reach "
            f"{_PICK_MIN_TOTAL_S:.0f}s."
        )
    )

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
        f"- Together the picks must total {min_total:.1f}-{_PICK_MAX_TOTAL_S:.0f} "
        "seconds and must not overlap each other in time."
        f"{material_note}\n"
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

    for turn in range(1, config.CLONE_OMNI_MAX_TURNS + 1):
        try:
            r = await _chat(
                video_id,
                f"pick_samples({speaker_id})",
                model=config.OMNI_MODEL, messages=messages, tools=tools,
                tool_choice="auto",
                # 同上：选段是判断，不是长推理。多轮重选时这个开关尤其重要
                # （最多 CLONE_OMNI_MAX_TURNS 轮，开着思考就是按轮数翻倍）。
                extra_body=_THINKING_OFF,
            )
        except Exception as e:
            logger.warning(f"[SamplePick] {speaker_id} turn {turn} API error: {e}")
            # 记账在 _chat 里已经做过了（成功拿到响应才记）—— 这里不需要再管。
            return None, f"omni 调用失败：{str(e)[:120]}"

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
            if total > _PICK_MAX_TOTAL_S:
                problems.append(
                    f"total {total:.1f}s exceeds {_PICK_MAX_TOTAL_S:.0f}s — drop some picks"
                )
            elif total < min_total:
                # 下限是 min_total（= min(建议下限, 可用素材)），所以这条只在模型
                # 【没把可用的都选上】时出现，仍然是一条可执行的指令。
                problems.append(
                    f"total {total:.1f}s is below {min_total:.1f}s — "
                    "pick more or longer segments"
                )

        if not problems:
            logger.info(
                f"[SamplePick] {speaker_id}: accepted after {turn} turn(s): "
                f"{[c['id'] for c in picked]} ({'; '.join(reasons)[:200]})"
            )
            return [c["id"] for c in picked], ""

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
    if available < _PICK_MIN_TOTAL_S:
        return None, (
            f"omni 连续 {config.CLONE_OMNI_MAX_TURNS} 轮没能挑出可用素材；这位说话人"
            f"在本工程里只有 {available:.1f}s 可用（低于建议下限 "
            f"{_PICK_MIN_TOTAL_S:.0f}s），音色质量可能不稳"
        )
    return None, (
        f"omni 连续 {config.CLONE_OMNI_MAX_TURNS} 轮没给出能通过校验的选择"
        "（候选可能确实都不合适）"
    )
