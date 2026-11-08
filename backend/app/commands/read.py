"""
只读命令：AI 用它们"看"项目，UI 也可以用。

设计原则：**不把整份数据塞进上下文**。AI 每轮拿到一份很短的概况（`overview`），要看
细节就调这里的命令，按需、分页、可筛选。每个返回值都用"第 N 句"编号 —— 那是人和
AI 共用的坐标系。
"""
import os
import wave
import io
import base64
from typing import Optional

from app import config, jobs, status, store, view
from app.commands.registry import CommandError, Ctx, command, resolve_line, resolve_speaker
from app.models import asr_audio_path, video_dir_path

TEXT_CAP = 160       # 列表里每句最多给多少字，完整内容用 get_lines


def _fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{s:04.1f}"


def line_row(state, seg, index: int, full: bool = False) -> dict:
    """一行的精简描述：AI 看这个，不看原始 JSON。"""
    tr = status.tr_status(state, seg)
    dub = status.tts_status(state, seg)
    slot = max(0.0, seg.end_time - seg.start_time)
    row = {
        "line": index + 1,
        "time": f"{_fmt_t(seg.start_time)}-{_fmt_t(seg.end_time)}",
        "slot_s": round(slot, 2),
        "speaker": seg.speaker_id,
        "text": seg.text if full else _cut(seg.text),
        "translation": seg.translated_text if full else _cut(seg.translated_text),
    }
    if seg.kind != "speech":
        row["kind"] = seg.kind
    if seg.reviewed_text and seg.reviewed_text != seg.text:
        row["reviewed"] = seg.reviewed_text if full else _cut(seg.reviewed_text)
    flags = [n for n, v in (("muted", seg.muted), ("hidden", seg.hidden)) if v]
    if flags:
        row["flags"] = flags
    if tr not in (status.FRESH, status.NONE):
        row["translation_status"] = tr
    if status.untranslated_suspect(state, seg):
        row["warning"] = f"译文看起来还是原文的文字，不像{state.target_language}——可能根本没翻译"
    if dub not in (status.FRESH, status.NONE):
        row["dub_status"] = dub
    if seg.actual_duration and slot:
        ratio = seg.actual_duration / slot
        row["dub_s"] = round(seg.actual_duration, 2)
        if ratio > 1.15 or ratio < 0.8:
            row["dub_vs_slot"] = f"{ratio:.2f}x"
    custom = {k: v for k, v in (("voice", seg.tts_voice), ("rate", seg.tts_rate),
                                 ("instruction", seg.tts_instruction)) if v is not None}
    if custom:
        row["dub_settings"] = custom
    return row


def _cut(text: Optional[str]) -> str:
    text = (text or "").strip()
    return text if len(text) <= TEXT_CAP else text[:TEXT_CAP] + "…"


def overview_data(state) -> dict:
    """概况：每轮对话都带上它，所以必须短。"""
    needs = status.needs(state)
    over, under, missing_tr = 0, 0, 0
    for seg in state.segments:
        if seg.actual_duration:
            slot = max(0.01, seg.end_time - seg.start_time)
            r = seg.actual_duration / slot
            over += r > 1.25
            under += r < 0.8
        missing_tr += status.tr_status(state, seg) in (status.MISSING, status.STALE)
    duration = max((s.end_time for s in state.segments), default=0.0)
    return {
        "project": state.filename,
        "stage": state.stage,
        "duration": _fmt_t(duration),
        "lines": len(state.segments),
        "non_speech_lines": sum(1 for s in state.segments if s.kind != "speech"),
        "settings": view.settings_view(state),
        "speakers": [
            {"id": sp.id, "name": sp.name, "lines": sum(1 for s in state.segments if s.speaker_id == sp.id),
             "voice": ("cloned" if status.clone_voice_id(state, sp) else sp.voice)}
            for sp in state.speakers
        ],
        "todo": {"translate": needs["translate"], "dub": needs["dub"]},
        "looks_untranslated": sum(1 for s in state.segments if status.untranslated_suspect(state, s)),
        "dub_overruns_slot": int(over),
        "dub_much_shorter_than_slot": int(under),
        "jobs": jobs.active_snapshot(state.video_id),
        "rev": state.rev,
    }


@command("get_overview", "项目概况：设置、说话人、还差多少活、有哪些任务在跑。先看这个。",
         readonly=True, group="read")
def get_overview(ctx: Ctx):
    return overview_data(ctx.state)


@command(
    "list_lines",
    "列出若干句（精简版，长文本会截断；要完整内容用 get_lines）。可按说话人、状态筛选，分页。",
    {"type": "object", "properties": {
        "from_line": {"type": "integer", "minimum": 1, "description": "起始行号（含），默认 1"},
        "to_line": {"type": "integer", "minimum": 1, "description": "结束行号（含），默认到末尾"},
        "speaker": {"type": "string", "description": "只看这个说话人（id 或名字）"},
        "filter": {"type": "string", "enum": ["all", "untranslated", "needs_dub", "overrun", "short", "non_speech", "muted", "hidden"],
                   "description": "overrun=配音比时间槽长很多；short=配音比时间槽短很多"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 80},
    }},
    readonly=True, group="read",
)
def list_lines(ctx: Ctx, from_line: int = 1, to_line: Optional[int] = None, speaker: Optional[str] = None,
               filter: str = "all", limit: int = 40):
    state = ctx.state
    sp_id = resolve_speaker(state, speaker).id if speaker else None
    hi = min(to_line or len(state.segments), len(state.segments))
    out, matched = [], 0
    for i in range(max(1, from_line) - 1, hi):
        seg = state.segments[i]
        if sp_id and seg.speaker_id != sp_id:
            continue
        if not _matches(state, seg, filter):
            continue
        matched += 1
        if len(out) < limit:
            out.append(line_row(state, seg, i))
    res = {"lines": out, "matched": matched, "total": len(state.segments)}
    if matched > len(out):
        res["note"] = f"只显示了前 {len(out)} 句，共 {matched} 句匹配；缩小范围或用 from_line 翻页"
    return res


def _matches(state, seg, flt: str) -> bool:
    if flt == "all":
        return True
    if flt == "untranslated":
        return status.tr_status(state, seg) in (status.MISSING, status.STALE)
    if flt == "needs_dub":
        return status.tts_status(state, seg) in (status.MISSING, status.STALE)
    if flt == "non_speech":
        return seg.kind != "speech"
    if flt == "muted":
        return seg.muted
    if flt == "hidden":
        return seg.hidden
    if flt in ("overrun", "short"):
        if not seg.actual_duration:
            return False
        r = seg.actual_duration / max(0.01, seg.end_time - seg.start_time)
        return r > 1.15 if flt == "overrun" else r < 0.8
    return True


@command(
    "get_lines", "取若干句的完整内容（原文、审听版、译文、配音设置、前后各一句作上下文）。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}, "description": "行号列表，最多 12 句"},
        "with_neighbors": {"type": "boolean", "description": "是否附上每句前后各一句（默认 true）"},
    }, "required": ["lines"]},
    readonly=True, group="read",
)
def get_lines(ctx: Ctx, lines: list, with_neighbors: bool = True):
    state = ctx.state
    if len(lines) > 12:
        raise CommandError("一次最多取 12 句；分几次取")
    out = []
    for ref in lines:
        i, seg = resolve_line(state, ref)
        item = line_row(state, seg, i, full=True)
        if with_neighbors:
            if i > 0:
                item["previous"] = line_row(state, state.segments[i - 1], i - 1)
            if i + 1 < len(state.segments):
                item["next"] = line_row(state, state.segments[i + 1], i + 1)
        out.append(item)
    return {"lines": out}


@command(
    "search_lines", "在原文/译文里搜关键词，返回匹配的句子。",
    {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": 80},
        "in": {"type": "string", "enum": ["text", "translation", "both"]},
    }, "required": ["query"]},
    readonly=True, group="read",
)
def search_lines(ctx: Ctx, query: str, **kw):
    where = kw.get("in", "both")
    q = query.strip().lower()
    state = ctx.state
    hits = []
    for i, seg in enumerate(state.segments):
        hay = []
        if where in ("text", "both"):
            hay += [seg.text, seg.reviewed_text]
        if where in ("translation", "both"):
            hay.append(seg.translated_text)
        if q and any(q in (h or "").lower() for h in hay):
            hits.append(line_row(state, seg, i))
    return {"matches": hits[:40], "count": len(hits)}


@command("list_voices", "目标语言下可选的系统音色，以及各说话人当前用的是哪个。",
         readonly=True, group="read")
def list_voices(ctx: Ctx):
    state = ctx.state
    pool = [{"id": v, "label": config.voice_label(v)} for v in config.voices_for_language(state.target_language)]
    return {
        "language": state.target_language,
        "supports_system_voices": config.system_voices_support(state.target_language),
        "voices": pool,
        "speakers": [
            {"id": sp.id, "name": sp.name, "voice": sp.voice, "cloned": bool(status.clone_voice_id(state, sp))}
            for sp in state.speakers
        ],
        "accents": list(config.CHINESE_ACCENTS) if state.target_language == "Chinese" else [],
    }


@command(
    "listen",
    "听一段原视频的声音（人声轨优先）。音频会附在下一条消息里。用来判断是不是音乐、有没有多人重叠、"
    "ASR 听错了什么。最长 60 秒。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1, "description": "听第几句（自动带前后各 1 秒）"},
        "start": {"type": "number", "minimum": 0, "description": "起点秒数（和 end 一起用，代替 line）"},
        "end": {"type": "number", "minimum": 0},
    }},
    readonly=True, group="read",
)
def listen(ctx: Ctx, line: Optional[int] = None, start: Optional[float] = None, end: Optional[float] = None):
    state = ctx.state
    if line is not None:
        _, seg = resolve_line(state, line)
        start, end = max(0.0, seg.start_time - 1.0), seg.end_time + 1.0
    if start is None or end is None:
        raise CommandError("要么给 line，要么同时给 start 和 end")
    if end <= start:
        raise CommandError("end 必须大于 start")
    if end - start > 60:
        raise CommandError("一次最多听 60 秒；缩小范围")
    path = asr_audio_path(ctx.video_id)
    if not path:
        raise CommandError("还没有可听的音轨（初始化还没跑到抽音频这一步）")
    wav_b64 = clip_wav_b64(path, start, end)
    # 音频本身不进返回值（要走特殊的消息通道），由 AI 循环取走 `_audio`。
    return {"listening": f"{_fmt_t(start)}-{_fmt_t(end)}", "_audio": wav_b64}


def clip_wav_b64(path: str, start: float, end: float) -> str:
    """从 16 kHz 单声道 WAV 里切一段，base64。直接按采样位置读，不起 ffmpeg。"""
    with wave.open(path, "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        total = w.getnframes()
        first = min(total, int(start * rate))
        last = min(total, int(end * rate))
        if last <= first:
            raise CommandError(f"这段时间超出了音频长度（{total / rate:.1f} 秒）")
        w.setpos(first)
        frames = w.readframes(last - first)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(channels); out.setsampwidth(width); out.setframerate(rate)
        out.writeframes(frames)
    return base64.b64encode(buf.getvalue()).decode()


@command("get_recent_changes", "最近发生的改动（谁改的、改了什么）。看用户在界面上刚做了什么。",
         {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 50}}},
         readonly=True, group="read")
def get_recent_changes(ctx: Ctx, limit: int = 15):
    events = store.events_since(ctx.video_id, 0)
    out = []
    for e in events[-limit:]:
        if e["type"] == "change":
            out.append({"seq": e["seq"], "by": e["actor"], "what": e["summary"], "lines": e.get("ids", [])})
        else:
            out.append({"seq": e["seq"], "job": e.get("label"), "phase": e.get("phase"), "error": e.get("error")})
    return {"changes": out}


@command("get_cost", "这个工程到目前为止的 API 花费。", readonly=True, group="read")
def get_cost(ctx: Ctx):
    from app.services import usage_service
    return usage_service.summary(ctx.video_id) or {"note": "还没有记录"}


def audio_exists(video_id: str) -> bool:
    return os.path.exists(os.path.join(video_dir_path(video_id), "extracted_audio.wav"))
