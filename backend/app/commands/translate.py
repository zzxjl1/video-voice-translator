"""
翻译类命令：直接写译文、让翻译模型重译（带要求）、项目级的翻译要求。

两条路并存，AI 按情况选：
  set_translation  AI 自己写（最灵活，适合"把第 5 句改成更口语的说法"）；
  retranslate      交给 DeepSeek 重译，可以带"长一点/短一点"或一句具体要求。

译文写进去时指纹和它一起落盘（`status.expected_tr_fp`），所以写进去的译文立刻是
"新鲜"的；之后原文/语言/要求再变，它才会过期。
"""
from typing import Optional

from app import jobs, status, store
from app.commands.registry import (
    CONFIRM_COST, CONFIRM_DESTROY, CommandError, Ctx, command, resolve_line, resolve_lines,
)
from app.commands.transcript import _guard


@command(
    "set_translation", "直接写某一句的译文（你自己改写）。可以带情感标签，如 [excited]。"
    "写完这一句的配音会过期，之后用 redub 补。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1},
        "text": {"type": "string", "maxLength": 1000},
    }, "required": ["line", "text"]},
)
def set_translation(ctx: Ctx, line: int, text: str):
    state = ctx.state
    i, seg = resolve_line(state, line)
    _guard(ctx, [seg])
    text = text.strip()
    if not text:
        raise CommandError("译文不能为空")
    if seg.kind != "speech":
        raise CommandError(f"第 {i + 1} 句被标成了{seg.kind}，不翻译。先 mark_non_speech kind=speech")
    if text == seg.translated_text:
        return {"line": i + 1, "unchanged": True}

    def apply(s):
        t = s.segment(seg.id)
        t.translated_text = text
        t.tr_fp = status.expected_tr_fp(s, t)
    ctx.change(apply, f"改第 {i + 1} 句译文")
    slot = max(0.1, seg.end_time - seg.start_time)
    return {"line": i + 1, "translation": text, "slot_s": round(slot, 2),
            "note": "配音已过期；调用 redub 重新合成"}


@command(
    "retranslate", "让翻译模型重译若干句。可以要求更长/更短，或附一句具体要求（只对这次生效）。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "要重译的行号；不给 = 所有缺失/过期的"},
        "length": {"type": "string", "enum": ["longer", "shorter"], "description": "更长 / 更短"},
        "instruction": {"type": "string", "maxLength": 300, "description": "对这次重译的具体要求，如“语气更随意，用口语”"},
    }},
    confirm=CONFIRM_COST, group="job",
)
async def retranslate(ctx: Ctx, lines: Optional[list] = None, length: Optional[str] = None,
                      instruction: str = ""):
    state = ctx.state
    ids = [s.id for s in resolve_lines(state, lines)] if lines else None
    if ids:
        _guard(ctx, [state.segment(i) for i in ids])
    job = await jobs.run(
        ctx.video_id, "translate", "翻译", ctx.actor,
        lambda job: jobs.translate_lines(ctx.video_id, job, ids=ids, length_hint=length, extra_prompt=instruction),
    )
    failed = await job.task
    after = store.require_state(ctx.video_id)
    shown = [after.segments.index(after.segment(i)) + 1 for i in (ids or []) if after.segment(i)]
    return {"retranslated": shown or "全部缺失/过期的行", "failed": len(failed or []),
            "note": "配音已过期；调用 redub 重新合成"}


@command(
    "set_custom_prompt", "设置整个项目的翻译要求（如“保留专有名词原文”“语气正式”）。"
    "会让所有译文过期，之后需要 retranslate。",
    {"type": "object", "properties": {"prompt": {"type": "string", "maxLength": 500}}, "required": ["prompt"]},
    confirm=CONFIRM_DESTROY,
)
def set_custom_prompt(ctx: Ctx, prompt: str):
    prompt = prompt.strip()
    if prompt == ctx.state.custom_prompt:
        return {"unchanged": True}

    def apply(s):
        s.custom_prompt = prompt
    ctx.change(apply, "修改翻译要求" if prompt else "清除翻译要求")
    return {"custom_prompt": prompt, "stale_translations": status.needs(ctx.state)["translate"],
            "note": "所有译文已过期；调用 retranslate 重译"}
