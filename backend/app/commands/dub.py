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
from app.bus import bus
from app.commands.transcript import _guard
from app.commands.guards import _need_balance


@command(
    "set_dub_style", "调某几句的配音：语气（自然语言）、语速、单句音色。给 null/空串表示清除该项、回到默认。"
    "调完这些句的配音过期，之后 redub。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "instruction": {"type": "string", "maxLength": 100,
                        "description": "语气指令，如“请用悲伤的语气慢慢说”“语速快一点，带点兴奋”。必须与译文是同一种语言的说法习惯；空串=清除"},
        "rate": {"type": "number", "minimum": 0, "maximum": 2.0, "description": "语速倍率 0.5~2.0，1.0 为正常；0 = 清除（回到按时间槽自动估算）"},
        "voice": {"type": "string", "description": "单句音色 id（见 list_voices）；空串=清除（回到跟随说话人）"},
        "clone_candidate": {"type": "string", "description": "单句改用这些句子的说话人的某个候选克隆音色（候选 id，"
                                                              "见说话人的 candidates）；和 voice 二选一"},
    }, "required": ["lines"]},
)
def set_dub_style(ctx: Ctx, lines: list, instruction: Optional[str] = None, rate: Optional[float] = None,
                  voice: Optional[str] = None, clone_candidate: Optional[str] = None):
    state = ctx.state
    if state.dubbing_mode == "off":
        raise CommandError("当前是仅字幕模式，没有配音；先把 dubbing_mode 改成 remote")
    if state.dubbing_mode == "browser":
        # 本地 Kokoro 没有语气 / 语速 / 克隆音色这些控制，指纹里也不含它们：改了不会生效，也不会过期
        raise CommandError("浏览器配音不支持调语气 / 语速；单句换音色用 set_line_browser_voice")
    segs = resolve_lines(state, lines)
    _guard(ctx, segs)
    if instruction is None and rate is None and voice is None and clone_candidate is None:
        raise CommandError("至少给 instruction / rate / voice / clone_candidate 之一")
    if clone_candidate is not None:
        if voice is not None:
            raise CommandError("voice 和 clone_candidate 只能给一个")
        owners = {x.speaker_id for x in segs}
        if len(owners) != 1:
            raise CommandError("clone_candidate 只能用在同一个说话人的句子上")
        sp = state.speaker(owners.pop())
        cand = next((c for c in (sp.clone_candidates if sp else []) if c.get("id") == clone_candidate), None)
        if cand is None:
            raise CommandError(f"{sp.name if sp else '该说话人'} 没有候选 {clone_candidate!r}")
        if cand.get("target_model") != config.TTS_MODEL:
            raise CommandError("这个候选是按别的 TTS 模型克隆的，不能用了；重新生成")
        voice = cand["voice_id"]
    if rate is not None and rate != 0 and rate < config.TTS_RATE_MIN:
        raise CommandError(f"语速要在 {config.TTS_RATE_MIN}~{config.TTS_RATE_MAX} 之间（或 0 表示清除），收到 {rate}")
    if voice and clone_candidate is None:
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


def _request_browser_dub(ctx: Ctx, state, ids: Optional[list], force: bool) -> dict:
    """
    本地配音模式：合成在用户的浏览器里做，服务端合成不了。所以这里不开任务，而是给页面发一个
    `browser_dub` 界面意图，编辑器收到后跑本地合成（和点"补"是同一条路径，免费）。

    命令返回时还没合成完 —— 返回值里写明，免得 AI 把"已请求"说成"已配好"。
    """
    want = set(ids) if ids else None
    todo = [s for s in state.segments
            if status.dubbable(state, s) and (want is None or s.id in want)
            and (force or status.tts_status(state, s) in (status.MISSING, status.STALE))]
    if not todo:
        return {"queued": 0, "note": "没有需要配音的句子（该配的都已是最新）"}
    if not bus.has_subscribers(ctx.video_id):
        raise CommandError(
            "本地配音是在用户的浏览器页面里合成的，但现在没有打开这个工程的页面，没法替他合成",
            hint="让用户打开这个工程，点顶栏的「补」；或等他回来再试",
        )
    ctx.ui("browser_dub", line_ids=[s.id for s in todo], force=bool(force))
    return {
        "queued": len(todo),
        "lines": [state.segments.index(s) + 1 for s in todo],
        "async": True,
        "note": "已请用户的页面在浏览器里合成这些句子（本地合成，免费）。命令返回时还没合成完，"
                "不要说已配好；之后看概览里还差多少句配音来确认。合成失败的句子会在页面上提示用户。",
    }


@command(
    "redub", "重新合成若干句的配音。不给 lines = 所有缺失/过期的句子。远端配音会调用 TTS（花钱，要先确认）；"
             "本地配音（浏览器配音）模式下是请用户的页面在浏览器里合成：免费，但异步，返回时还没合成完。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "行号；不给 = 所有缺失/过期的"},
        "force": {"type": "boolean", "description": "即使配音是新鲜的也强制重做（随机性让同一句每次略有不同）"},
    }},
    confirm=CONFIRM_COST, confirm_unless=lambda s: s.dubbing_mode == "browser", group="job",
)
async def redub(ctx: Ctx, lines: Optional[list] = None, force: bool = False):
    state = ctx.state
    if state.dubbing_mode == "remote":
        _need_balance(ctx, "重新配音")
    if state.dubbing_mode == "off":
        raise CommandError("当前是仅字幕模式，没有配音；先把 dubbing_mode 改成 remote")
    ids = [s.id for s in resolve_lines(state, lines)] if lines else None
    if ids:
        _guard(ctx, [state.segment(i) for i in ids])
        missing_tr = [state.segments.index(state.segment(i)) + 1 for i in ids
                      if status.tr_status(state, state.segment(i)) in (status.MISSING, status.STALE)]
        if missing_tr:
            raise CommandError(f"第 {missing_tr} 句的译文还缺/已过期，先 retranslate 再配音")
    if state.dubbing_mode == "browser":
        return _request_browser_dub(ctx, state, ids, force)
    job = await jobs.run(
        ctx.video_id, "dub", "配音", ctx.actor,
        lambda job: jobs.dub_lines(ctx.video_id, job, ids=ids, force=force),
    )
    failed = await job.task
    if job.error:                                   # 见 retranslate 里的说明
        raise CommandError(f"配音任务失败：{job.error}")
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
