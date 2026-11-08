"""
转写类命令：改原文、拆句、合并、调时间、标记非语音、改说话人、增删行。

这是"不再被 ASR 的死板输出卡住"的地方：音乐被误识成话 → `mark_non_speech`；
两个人同时说话被并成一句 → `split_line` + `set_speaker`；时间切点歪了 → `retime_line`。

联动靠指纹，这里没有任何"手动作废译文/配音"的代码：改了原文，译文指纹自然对不上；
改了时间槽，配音拟合的目标变了，下一次配音会重新拟合。需要的话用 `retranslate` /
`redub` 把过期的补上。

被任务占用的行不能改（任务正在翻译/配音它们），报错里说明是哪个任务。
"""
import uuid
from typing import Optional

from app import jobs
from app.commands.registry import (
    CONFIRM_COST, CONFIRM_DESTROY, CommandError, Ctx, command, resolve_line, resolve_lines,
    resolve_speaker,
)
from app.models import Segment, Speaker


def _guard(ctx: Ctx, segs) -> None:
    """被任务占用的行不能改，并说明是谁占用的。"""
    locked = jobs.locked_lines(ctx.video_id)
    busy = [(s, locked[s.id]) for s in segs if s.id in locked]
    if busy:
        index = {s.id: i + 1 for i, s in enumerate(ctx.state.segments)}
        nums = "、".join(str(index.get(s.id, "?")) for s, _ in busy[:6])
        job = busy[0][1]
        raise CommandError(
            f"第 {nums} 句正被「{job.label}」处理中，暂时不能改",
            hint="等它结束，或先取消这个任务（cancel_job）",
        )


def _new_id() -> str:
    return f"seg-{uuid.uuid4().hex[:8]}"


@command(
    "edit_source", "改某一句的原文（识别错了的字）。译文会因此过期，之后用 retranslate 补。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1},
        "text": {"type": "string", "maxLength": 2000},
    }, "required": ["line", "text"]},
)
def edit_source(ctx: Ctx, line: int, text: str):
    state = ctx.state
    i, seg = resolve_line(state, line)
    _guard(ctx, [seg])
    text = text.strip()
    if not text:
        raise CommandError("原文不能为空；这一句不要了请用 delete_lines，或用 mark_non_speech 标成非语音")
    if text == seg.text:
        return {"line": i + 1, "unchanged": True}

    def apply(s):
        target = s.segment(seg.id)
        target.text = text
        target.reviewed_text = ""          # 审听版描述的是旧原文
    ctx.change(apply, f"改第 {i + 1} 句原文")
    return {"line": i + 1, "text": text, "note": "译文已过期，需要的话调用 retranslate"}


@command(
    "split_line", "把一句拆成两句（比如两个人同时说话被并成一句，或一句太长）。"
    "给出拆分点：原文在哪里断开、时间在哪里切开。两半的译文和配音都需要重做。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1},
        "text_first": {"type": "string", "description": "前半句的原文"},
        "text_second": {"type": "string", "description": "后半句的原文"},
        "at_seconds": {"type": "number", "description": "切分的时间点（秒，绝对时间）；不给则按字数比例估算"},
        "second_speaker": {"type": "string", "description": "后半句的说话人（不给则同前半句）"},
    }, "required": ["line", "text_first", "text_second"]},
)
def split_line(ctx: Ctx, line: int, text_first: str, text_second: str, at_seconds: Optional[float] = None,
               second_speaker: Optional[str] = None):
    state = ctx.state
    i, seg = resolve_line(state, line)
    _guard(ctx, [seg])
    a, b = text_first.strip(), text_second.strip()
    if not a or not b:
        raise CommandError("两半原文都不能为空")
    if at_seconds is None:
        at_seconds = seg.start_time + (seg.end_time - seg.start_time) * len(a) / max(1, len(a) + len(b))
    if not seg.start_time + 0.2 <= at_seconds <= seg.end_time - 0.2:
        raise CommandError(
            f"切分时间 {at_seconds:.2f}s 要落在这一句内部（{seg.start_time:.2f}~{seg.end_time:.2f}s），"
            "且两半各至少 0.2 秒"
        )
    speaker2 = resolve_speaker(state, second_speaker).id if second_speaker else seg.speaker_id
    new_id = _new_id()

    def apply(s):
        first = s.segment(seg.id)
        idx = s.segments.index(first)
        second = Segment(id=new_id, speaker_id=speaker2, start_time=at_seconds, end_time=first.end_time,
                         text=b, kind=first.kind, muted=first.muted, hidden=first.hidden,
                         tts_rate=first.tts_rate, tts_instruction=first.tts_instruction)
        first.end_time = at_seconds
        first.text = a
        for part in (first,):
            part.reviewed_text = ""
        s.segments.insert(idx + 1, second)
    ctx.change(apply, f"把第 {i + 1} 句拆成两句")
    return {"first": i + 1, "second": i + 2, "split_at": round(at_seconds, 2),
            "note": "两半的译文都需要重做：retranslate lines=[%d,%d]" % (i + 1, i + 2)}


@command(
    "merge_lines", "把相邻的几句合成一句（比如一句话被 ASR 切碎了）。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "要合并的行号，必须相邻"},
    }, "required": ["lines"]},
)
def merge_lines(ctx: Ctx, lines: list):
    state = ctx.state
    segs = resolve_lines(state, lines)
    if len(segs) < 2:
        raise CommandError("至少要两句才能合并")
    idxs = sorted(state.segments.index(s) for s in segs)
    if idxs != list(range(idxs[0], idxs[0] + len(idxs))):
        raise CommandError("只能合并相邻的句子")
    _guard(ctx, segs)
    speakers = {s.speaker_id for s in segs}
    if len(speakers) > 1:
        raise CommandError(f"这几句不是同一个说话人（{sorted(speakers)}），先用 set_speaker 统一再合并")
    keep_id = state.segments[idxs[0]].id
    drop = {state.segments[i].id for i in idxs[1:]}
    joiner = "" if state.target_language == "" else " "

    def apply(s):
        group = [s.segment(state.segments[i].id) for i in idxs]
        first = group[0]
        first.text = joiner.join(g.text.strip() for g in group)
        first.end_time = max(g.end_time for g in group)
        first.reviewed_text = ""
        s.segments = [x for x in s.segments if x.id not in drop]
        assert s.segment(keep_id) is first
    ctx.change(apply, f"合并第 {idxs[0] + 1}~{idxs[-1] + 1} 句")
    return {"merged_into": idxs[0] + 1, "removed": len(drop)}


@command(
    "retime_line", "调某一句的起止时间（切点歪了）。不能和相邻句重叠太多。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1},
        "start": {"type": "number", "minimum": 0},
        "end": {"type": "number", "minimum": 0},
    }, "required": ["line"]},
)
def retime_line(ctx: Ctx, line: int, start: Optional[float] = None, end: Optional[float] = None):
    state = ctx.state
    i, seg = resolve_line(state, line)
    _guard(ctx, [seg])
    new_start = seg.start_time if start is None else start
    new_end = seg.end_time if end is None else end
    if new_end - new_start < 0.2:
        raise CommandError("一句至少要 0.2 秒")
    if i > 0 and new_start < state.segments[i - 1].start_time:
        raise CommandError("起点不能早于上一句的起点；要整体调整请先 merge/split")
    if i + 1 < len(state.segments) and new_end > state.segments[i + 1].end_time:
        raise CommandError("终点不能晚于下一句的终点")

    def apply(s):
        t = s.segment(seg.id)
        t.start_time, t.end_time = new_start, new_end
    ctx.change(apply, f"调第 {i + 1} 句时间")
    return {"line": i + 1, "start": round(new_start, 2), "end": round(new_end, 2),
            "note": "时间槽变了，这一句的配音会重新拟合（redub）"}


@command(
    "mark_non_speech", "把某些句标成音乐/噪音（ASR 把它们误识成了话）：不翻译、不配音、不出字幕。"
    "标回 speech 就恢复。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "kind": {"type": "string", "enum": ["music", "noise", "speech"]},
    }, "required": ["lines", "kind"]},
)
def mark_non_speech(ctx: Ctx, lines: list, kind: str):
    state = ctx.state
    segs = resolve_lines(state, lines)
    _guard(ctx, segs)
    ids = {s.id for s in segs}

    def apply(s):
        for x in s.segments:
            if x.id in ids:
                x.kind = kind
    nums = "、".join(str(state.segments.index(s) + 1) for s in segs)
    ctx.change(apply, f"把第 {nums} 句标为{ {'music': '音乐', 'noise': '噪音', 'speech': '语音'}[kind] }")
    return {"lines": [state.segments.index(s) + 1 for s in segs], "kind": kind}


@command(
    "set_speaker", "改某些句的说话人（diarization 分错了）。说话人不存在就新建。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "speaker": {"type": "string", "description": "说话人 id 或名字；不存在则新建"},
    }, "required": ["lines", "speaker"]},
)
def set_speaker(ctx: Ctx, lines: list, speaker: str):
    state = ctx.state
    segs = resolve_lines(state, lines)
    _guard(ctx, segs)
    speaker = str(speaker).strip()
    if not speaker:
        raise CommandError("说话人不能为空")
    existing = next((sp for sp in state.speakers if sp.id == speaker or sp.name == speaker), None)
    target_id = existing.id if existing else speaker
    ids = {s.id for s in segs}

    def apply(s):
        if s.speaker(target_id) is None:
            s.speakers.append(Speaker(id=target_id, name=speaker if not speaker.isdigit() else f"Speaker {speaker}"))
        for x in s.segments:
            if x.id in ids:
                x.speaker_id = target_id
        used = {x.speaker_id for x in s.segments}
        s.speakers = [sp for sp in s.speakers if sp.id in used]      # 没人用的说话人不留
    nums = "、".join(str(state.segments.index(x) + 1) for x in segs)
    ctx.change(apply, f"把第 {nums} 句改给说话人 {speaker}")
    return {"lines": [state.segments.index(x) + 1 for x in segs], "speaker": target_id,
            "created": existing is None,
            "note": "这些句子的配音音色会随说话人变化而过期（redub）"}


@command(
    "rename_speaker", "给说话人改个显示名（只是名字，不影响音色）。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"}, "name": {"type": "string", "maxLength": 40},
    }, "required": ["speaker", "name"]},
)
def rename_speaker(ctx: Ctx, speaker: str, name: str):
    sp = resolve_speaker(ctx.state, speaker)
    name = name.strip()
    if not name:
        raise CommandError("名字不能为空")

    def apply(s):
        s.speaker(sp.id).name = name
    ctx.change(apply, f"把说话人 {sp.name} 改名为 {name}")
    return {"speaker": sp.id, "name": name}


@command(
    "insert_line", "在某句后面插入一句新的（ASR 漏掉的话）。需要给出时间和原文。",
    {"type": "object", "properties": {
        "after_line": {"type": "integer", "minimum": 0, "description": "插在第几句之后；0 = 插到最前面"},
        "start": {"type": "number", "minimum": 0}, "end": {"type": "number", "minimum": 0},
        "text": {"type": "string", "maxLength": 2000},
        "speaker": {"type": "string"},
    }, "required": ["after_line", "start", "end", "text", "speaker"]},
)
def insert_line(ctx: Ctx, after_line: int, start: float, end: float, text: str, speaker: str):
    state = ctx.state
    if not 0 <= after_line <= len(state.segments):
        raise CommandError(f"after_line 要在 0~{len(state.segments)} 之间")
    if end - start < 0.2:
        raise CommandError("一句至少要 0.2 秒")
    text = text.strip()
    if not text:
        raise CommandError("原文不能为空")
    sp = resolve_speaker(state, speaker)
    new_id = _new_id()

    def apply(s):
        s.segments.insert(after_line, Segment(id=new_id, speaker_id=sp.id, start_time=start, end_time=end, text=text))
    ctx.change(apply, f"在第 {after_line} 句后插入新句")
    return {"line": after_line + 1, "id": new_id}


@command(
    "delete_lines", "删除若干句（确实不该存在的，比如纯噪音误识成的话）。可以撤销。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
    }, "required": ["lines"]},
    confirm=CONFIRM_COST,
)
def delete_lines(ctx: Ctx, lines: list):
    state = ctx.state
    segs = resolve_lines(state, lines)
    _guard(ctx, segs)
    ids = {s.id for s in segs}
    if len(ids) >= len(state.segments):
        raise CommandError("不能把所有句子都删掉")
    nums = [state.segments.index(s) + 1 for s in segs]

    def apply(s):
        s.segments = [x for x in s.segments if x.id not in ids]
        used = {x.speaker_id for x in s.segments}
        s.speakers = [sp for sp in s.speakers if sp.id in used]
    ctx.change(apply, f"删除第 {'、'.join(map(str, nums))} 句")
    return {"deleted": nums}


@command(
    "rerun_asr", "整段重新识别（会清掉现有的转写、译文、配音，重新来一遍，花钱）。"
    "只在转写整体就不对（语言选错、整段漏掉）时用；零星的错请用 edit_source / split_line。",
    {"type": "object", "properties": {}},
    confirm=CONFIRM_DESTROY, group="job",
)
async def rerun_asr(ctx: Ctx):
    from app.commands.project import start_initialize
    state = ctx.state
    if jobs.find(ctx.video_id, kind="init"):
        raise CommandError("初始化正在进行")
    n = len(state.segments)

    def clear(s):
        s.segments = []
        s.speakers = [Speaker(id=sp.id, name=sp.name, voice=sp.voice, clone=sp.clone) for sp in s.speakers]
    ctx.change(clear, f"清除转写（{n} 句），准备重新识别", undoable=False)
    await start_initialize(ctx)
    return {"started": True, "cleared_lines": n}
