"""
配音类命令：语气/语速/音色、重新合成、静音、隐藏。

每一句的配音可以单独调：
  · `instruction` 自然语言语气指令（官方文档确认 3.1-flash 支持，实测"悲伤慢速"让同一句
    从 2.8s 变成 5.75s，"兴奋快速"变短）；会与项目的方言指令合并，总长不超过 128 字符；
  · `rate` 语速倍率 0.5–2.0；
  · `voice` 本行单独换音色。
设了这些之后配音指纹变了 → 这一句过期 → `redub` 补上。

注意：语气指令改变的不只是"听感"，还有**时长**。AI 调完语气后应该看一下 `dub_vs_slot`，
超出时间槽就调语速或重译短一点。
"""
from typing import Optional

from app import config, jobs, status, store
from app.commands.registry import (
    CONFIRM_COST, CommandError, Ctx, command, resolve_lines,
)
from app.commands.transcript import _guard


@command(
    "set_dub_style", "调某几句的配音：语气（自然语言）、语速、单句音色。给 null/空串表示清除该项、回到默认。"
    "调完这些句的配音过期，之后 redub。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "instruction": {"type": "string", "maxLength": 100,
                        "description": "语气指令，如“请用悲伤的语气慢慢说”“语速快一点，带点兴奋”。必须与译文是同一种语言的说法习惯；空串=清除"},
        "rate": {"type": "number", "minimum": 0, "maximum": 2.0, "description": "语速倍率 0.5~2.0，1.0 为正常；0 = 清除（回到按时间槽自动估算）"},
        "voice": {"type": "string", "description": "单句音色 id（见 list_voices）；空串=清除"},
    }, "required": ["lines"]},
)
def set_dub_style(ctx: Ctx, lines: list, instruction: Optional[str] = None, rate: Optional[float] = None,
                  voice: Optional[str] = None):
    state = ctx.state
    segs = resolve_lines(state, lines)
    _guard(ctx, segs)
    if instruction is None and rate is None and voice is None:
        raise CommandError("至少给 instruction / rate / voice 之一")
    if rate is not None and rate != 0 and rate < config.TTS_RATE_MIN:
        raise CommandError(f"语速要在 {config.TTS_RATE_MIN}~{config.TTS_RATE_MAX} 之间（或 0 表示清除），收到 {rate}")
    if voice:
        pool = config.voices_for_language(state.target_language)
        if voice not in pool:
            raise CommandError(f"音色 {voice!r} 不能用于{state.target_language}", hint="用 list_voices 看可选的")
    ids = {s.id for s in segs}

    def apply(s):
        for x in s.segments:
            if x.id not in ids:
                continue
            if instruction is not None:
                x.tts_instruction = instruction.strip() or None
            if rate is not None:
                x.tts_rate = rate if rate > 0 else None
            if voice is not None:
                x.tts_voice = voice or None
    nums = "、".join(str(state.segments.index(x) + 1) for x in segs)
    bits = [b for b in (
        f"语气「{instruction}」" if instruction else ("清除语气" if instruction == "" else None),
        f"语速 {rate}" if rate else ("清除语速" if rate == 0 else None),
        "换音色" if voice else ("清除单句音色" if voice == "" else None)) if b]
    ctx.change(apply, f"调第 {nums} 句配音（{'，'.join(bits)}）")
    return {"lines": [state.segments.index(x) + 1 for x in segs],
            "note": "配音已过期；调用 redub 重新合成，之后看 dub_vs_slot 是否还在时间槽内"}


@command(
    "redub", "重新合成若干句的配音（会调用 TTS，花钱）。不给 lines = 所有缺失/过期的句子。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "行号；不给 = 所有缺失/过期的"},
        "force": {"type": "boolean", "description": "即使配音是新鲜的也强制重做（随机性让同一句每次略有不同）"},
    }},
    confirm=CONFIRM_COST, group="job",
)
async def redub(ctx: Ctx, lines: Optional[list] = None, force: bool = False):
    state = ctx.state
    if not state.enable_dubbing:
        raise CommandError("这个项目是「仅字幕」模式，没有配音；先 set_settings enable_dubbing=true")
    ids = [s.id for s in resolve_lines(state, lines)] if lines else None
    if ids:
        _guard(ctx, [state.segment(i) for i in ids])
        missing_tr = [state.segments.index(state.segment(i)) + 1 for i in ids
                      if status.tr_status(state, state.segment(i)) in (status.MISSING, status.STALE)]
        if missing_tr:
            raise CommandError(f"第 {missing_tr} 句的译文还缺/已过期，先 retranslate 再配音")
    job = await jobs.run(
        ctx.video_id, "dub", "配音", ctx.actor,
        lambda job: jobs.dub_lines(ctx.video_id, job, ids=ids, force=force),
    )
    failed = await job.task
    after = store.require_state(ctx.video_id)
    rows = []
    for i in (ids or []):
        seg = after.segment(i)
        if seg is None:
            continue
        slot = max(0.01, seg.end_time - seg.start_time)
        rows.append({"line": after.segments.index(seg) + 1, "dub_s": seg.actual_duration,
                     "slot_s": round(slot, 2),
                     "fits": bool(seg.actual_duration and 0.8 <= seg.actual_duration / slot <= 1.25)})
    return {"failed": len(failed or []), **({"results": rows} if rows else {})}


def _flag(ctx: Ctx, lines: list, flag: str, value: bool, word: str):
    state = ctx.state
    segs = resolve_lines(state, lines)
    ids = {s.id for s in segs}

    def apply(s):
        for x in s.segments:
            if x.id in ids:
                setattr(x, flag, value)
    nums = "、".join(str(state.segments.index(x) + 1) for x in segs)
    ctx.change(apply, f"{word}第 {nums} 句")
    return {"lines": [state.segments.index(x) + 1 for x in segs], flag: value}


@command(
    "set_muted", "静音/取消静音某些句：静音的句子不配音，成片里这一格没有声音。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "muted": {"type": "boolean"},
    }, "required": ["lines", "muted"]},
)
def set_muted(ctx: Ctx, lines: list, muted: bool):
    return _flag(ctx, lines, "muted", muted, "静音" if muted else "取消静音")


@command(
    "set_hidden", "隐藏/取消隐藏某些句的字幕：隐藏的句子不出字幕，但配音照常。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "hidden": {"type": "boolean"},
    }, "required": ["lines", "hidden"]},
)
def set_hidden(ctx: Ctx, lines: list, hidden: bool):
    return _flag(ctx, lines, "hidden", hidden, "隐藏" if hidden else "取消隐藏")
