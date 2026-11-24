"""
只读命令：AI 用它们"看"项目，UI 也可以用。

设计原则：**不把整份数据塞进上下文**。AI 每轮拿到一份很短的概况（`overview`），要看
细节就调这里的命令，按需、分页、可筛选。每个返回值都用"第 N 句"编号 —— 那是人和
AI 共用的坐标系。
"""
import os
import subprocess
import wave
import io
import base64
from typing import Optional

from app import config, jobs, status, store, view
from app.services import audio_energy
from app.commands.registry import CommandError, Ctx, command, resolve_line, resolve_speaker
from app.models import original_audio_path, tts_audio_path, video_dir_path, vocals_audio_path

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
    "find_uncovered_sounds",
    "找出音轨上有声音、但没有任何一句台词覆盖的时间段：ASR 漏掉的笑声、叹息、咳嗽，或被吞掉的话。"
    "返回每段的起止和\"在第几句之后\"。看到之后用 listen 听一下，再决定：是人发出的非语言声 → "
    "insert_line kind=vocal（做过人声分离的工程会保留这段原声）；是话 → insert_line（补上这句）。"
    "压在别人说话中间的声音（比如说话时咳嗽）这里找不到，因为那里本来就有台词。",
    {"type": "object", "properties": {}},
    readonly=True, group="read",
)
def find_uncovered_sounds(ctx: Ctx):
    sounds = audio_energy.find_candidates(ctx.video_id, ctx.state.segments)
    if sounds is None:
        raise CommandError("还没有可分析的音轨（初始化还没跑到抽音频这一步）")
    shown = [{"after_line": s["after_line"], "start": s["start"], "end": s["end"],
              "seconds": round(s["end"] - s["start"], 2)} for s in sounds[:30]]
    result: dict = {"count": len(sounds), "sounds": shown}
    if len(sounds) > 30:
        result["note"] = f"只列出了前 30 段，共 {len(sounds)} 段"
    if not audio_energy.is_separated(ctx.video_id):
        result["warning"] = ("这个工程没做人声分离，分析的是整条原声：风声、背景音乐也会被算成\"有声音\"，"
                             "结果会偏多、边界不准。务必先 listen 确认再动手")
    return result


@command(
    "listen",
    "听一段声音，音频会附在下一条消息里。用来判断是不是音乐、有没有多人重叠、ASR 听错了什么、有没有笑声/风声/撞击声。"
    "最长 60 秒。track 选听哪条轨：original（默认）= 原视频的完整声音，含背景音乐和环境声；"
    "vocals = 只听人声轨（要做过人声分离才有）；dub = 听某一句配音出来的成品（必须给 line）。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1,
                 "description": "听第几句（original/vocals 自动带前后各 1 秒；dub 只听这一句的配音）"},
        "start": {"type": "number", "minimum": 0, "description": "起点秒数（和 end 一起用，代替 line；dub 不能用）"},
        "end": {"type": "number", "minimum": 0},
        "track": {"type": "string", "enum": ["original", "vocals", "dub"],
                  "description": "听哪条轨，默认 original"},
    }},
    readonly=True, group="read",
)
def listen(ctx: Ctx, line: Optional[int] = None, start: Optional[float] = None, end: Optional[float] = None,
           track: str = "original"):
    state = ctx.state
    if track == "dub":
        return _listen_dub(ctx, line, start, end)

    if line is not None:
        _, seg = resolve_line(state, line)
        start, end = max(0.0, seg.start_time - 1.0), seg.end_time + 1.0
    if start is None or end is None:
        raise CommandError("要么给 line，要么同时给 start 和 end")
    if end <= start:
        raise CommandError("end 必须大于 start")
    if end - start > 60:
        raise CommandError("一次最多听 60 秒；缩小范围")

    note = None
    if track == "vocals":
        path = vocals_audio_path(ctx.video_id)
        if not path:
            raise CommandError("这个工程没有人声轨（没做过人声分离）", hint="用 track=original 听原声")
        used = "vocals"
    else:
        path = original_audio_path(ctx.video_id)
        used = "original"
        if not path:
            path = vocals_audio_path(ctx.video_id)         # 没有原轨就退回人声轨，并如实告诉模型
            used = "vocals"
            note = "没有原声音轨，听到的是人声轨（背景音、环境声可能已被去掉）"
    if not path:
        raise CommandError("还没有可听的音轨（初始化还没跑到抽音频这一步）")
    wav_b64 = clip_wav_b64(path, start, end)
    # 音频本身不进返回值（要走特殊的消息通道），由 AI 循环取走 `_audio`。
    result = {"track": used, "listening": f"{_fmt_t(start)}-{_fmt_t(end)}", "_audio": wav_b64}
    if note:
        result["note"] = note
    return result


def _listen_dub(ctx: Ctx, line: Optional[int], start: Optional[float], end: Optional[float]):
    """听一句配音的成品。配音是 mp3，转成和其他轨一样的 16 kHz 单声道 wav 再交给模型。"""
    if line is None:
        raise CommandError("听配音要给 line（第几句）")
    if start is not None or end is not None:
        raise CommandError("track=dub 听的是整句配音，不能再给 start/end")
    state = ctx.state
    i, seg = resolve_line(state, line)
    dub = status.tts_status(state, seg)
    if dub == status.NONE:
        raise CommandError(f"第 {i + 1} 句不配音（静音、非语音、没有译文，或没开配音）")
    if dub == status.MISSING:
        raise CommandError(f"第 {i + 1} 句还没有配音", hint="先 redub 这一句")
    wav_b64, seconds = dub_wav_b64(tts_audio_path(ctx.video_id, seg.id))
    result = {"track": "dub", "listening": f"第 {i + 1} 句的配音（{seconds:.1f} 秒）", "_audio": wav_b64}
    if dub == status.STALE:
        result["note"] = "这是旧的配音：译文或配音设置改过之后还没重新合成，听到的不是最新的"
    return result


def dub_wav_b64(path: str) -> tuple[str, float]:
    """
    mp3 → 16 kHz 单声道 wav（最多 60 秒），返回 (base64, 秒数)。

    ffmpeg 只输出裸 PCM，wav 头由 `wave` 自己写：让 ffmpeg 往管道里写 wav 的话，它没法回头
    改文件头，头里的长度是个巨大的占位值（实测解码出 13 万秒），下游会读错。
    """
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", path, "-t", "60", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CommandError(f"没能读出配音音频：{e}")
    if r.returncode != 0 or not r.stdout:
        raise CommandError("配音文件读不出来（可能已损坏），重新 redub 这一句试试")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
        out.writeframes(r.stdout)
    return base64.b64encode(buf.getvalue()).decode(), len(r.stdout) / 2 / 16000.0


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


@command("get_cost", "这个工程到目前为止用了多少积分（1 积分 ≈ 0.01 元）。", readonly=True, group="read")
def get_cost(ctx: Ctx):
    from app.billing import service as billing
    data = billing.project_summary(ctx.video_id)
    if not data["calls"]:
        return {"note": "这个工程还没有计费记录（本地配音 / 分离 / 导出不收费）"}
    data.pop("unpriced_calls", None)      # 内部口径，不对外
    return data


def audio_exists(video_id: str) -> bool:
    return os.path.exists(os.path.join(video_dir_path(video_id), "extracted_audio.wav"))
