"""
项目级命令：设置、初始化/任务控制、导出、撤销、界面联动。

设置分两类，对已有工作的影响不同，所以确认级别不同：
  · 只影响后续（口音、克隆开关…）：配音过期 → 直接改；
  · 推翻已有的（目标语言、翻译要求、是否出配音）：译文/配音大面积过期 → 需要确认。
"""
from typing import Optional

from app import config, jobs, status, store
from app.commands.registry import (
    CONFIRM_COST, CONFIRM_DESTROY, CommandError, Ctx, command, resolve_line,
)

# 设置项 → (类型, 是否会让已有译文大面积过期)
_SETTINGS = {
    "target_language": ("string", True),
    "accent": ("string", False),
    "custom_prompt": ("string", True),
    "mm_enhance": ("boolean", False),
    "enable_voice_clone": ("boolean", False),
    "clone_smart_pick": ("boolean", False),
    "enable_dubbing": ("boolean", False),
}


@command(
    "set_settings",
    "改项目设置。目标语言/配音开关等。注意：改目标语言会让所有译文和配音过期（需要确认并重做）。",
    {"type": "object", "properties": {
        "target_language": {"type": "string", "description": "目标语言，如 Chinese / English / Japanese"},
        "accent": {"type": "string", "description": "中文方言（仅目标语言为 Chinese 时有效）：" + "、".join(config.CHINESE_ACCENTS) + "；空串=普通话"},
        "mm_enhance": {"type": "boolean", "description": "omni 审听增强（纠错 + 语气标签）；只影响之后的审听"},
        "enable_voice_clone": {"type": "boolean"},
        "clone_smart_pick": {"type": "boolean"},
        "enable_dubbing": {"type": "boolean", "description": "false = 仅字幕模式，不出配音"},
    }},
    confirm=CONFIRM_DESTROY,
)
def set_settings(ctx: Ctx, **patch):
    state = ctx.state
    if not patch:
        raise CommandError("至少给一项要改的设置")
    changed = {k: v for k, v in patch.items() if getattr(state, k) != (v or None if k == "accent" else v)}
    if not changed:
        return {"unchanged": True}
    if "accent" in changed and changed["accent"] and changed["accent"] not in config.CHINESE_ACCENTS:
        raise CommandError(f"不支持的方言 {changed['accent']!r}，可选：{'、'.join(config.CHINESE_ACCENTS)}")

    def apply(s):
        for k, v in changed.items():
            setattr(s, k, (v or None) if k == "accent" else v)
        if "target_language" in changed:
            # 音色 id 是语言专属的：旧语言的音色在新语言下会被服务端直接拒绝。
            for sp in s.speakers:
                sp.voice = None
            for seg in s.segments:
                seg.tts_voice = None
        if changed.get("enable_dubbing") is False:
            s.enable_voice_clone = False
    ctx.change(apply, "改设置：" + "、".join(changed))
    after = ctx.state
    return {"changed": list(changed), "todo": status.needs(after),
            "note": "译文/配音已按新设置过期；调用 retranslate 和 redub 补上" if status.needs(after)["translate"] else None}


@command("get_settings", "当前项目设置。", readonly=True, group="read")
def get_settings(ctx: Ctx):
    from app import view
    return view.settings_view(ctx.state)


async def start_initialize(ctx: Ctx):
    """启动（或重启）初始化任务。其它命令（rerun_asr）也要用它。"""
    server_url = config.SERVER_URL_BASE
    return await jobs.run(
        ctx.video_id, "init", "初始化", ctx.actor,
        lambda job: jobs.initialize(ctx.video_id, job, server_url), exclusive=True,
    )


@command(
    "run_initialize", "运行初始化（转写→审听→翻译→克隆→配音），只做还缺的部分。失败后重试用它。",
    confirm=CONFIRM_COST, group="job",
)
async def run_initialize(ctx: Ctx):
    if jobs.find(ctx.video_id, kind="init"):
        raise CommandError("初始化已经在进行了")
    job = await start_initialize(ctx)
    return {"job": job.id, "started": True}


@command(
    "list_jobs", "正在运行的任务。", readonly=True, group="read",
)
def list_jobs(ctx: Ctx):
    return {"jobs": jobs.active_snapshot(ctx.video_id)}


@command(
    "cancel_job", "取消正在运行的任务（当前这一次外部请求会跑完，之后不再发新的）。不给 job 就取消全部。",
    {"type": "object", "properties": {"job": {"type": "string"}}},
    group="job",
)
def cancel_job(ctx: Ctx, job: Optional[str] = None):
    n = jobs.cancel(ctx.video_id, job)
    if not n:
        raise CommandError("没有正在运行的任务")
    return {"cancelling": n}


@command(
    "undo", "撤销最近一次改动（用户或 AI 做的）。可以连续撤销。", group="edit",
)
def undo(ctx: Ctx):
    try:
        undone = store.undo(ctx.video_id, actor=ctx.actor)
    except store.NothingToUndo:
        raise CommandError("没有可撤销的改动")
    return {"undone": undone["summary"]}


@command(
    "export_video", "导出成片（配音混音 + 可选字幕）。需要先有配音（或仅字幕模式）。"
    "默认按项目保存的字幕方案；format 可以只对这一次导出覆盖（不会改项目的设置）。",
    {"type": "object", "properties": {
        "format": {"type": "string", "enum": ["off", "soft", "styled"],
                   "description": "这一次导出的字幕方式：off 不带 / soft 外挂软字幕(mp4) / styled 内嵌带样式(mkv)。烧录需要浏览器，不在这里。"},
    }},
    confirm=CONFIRM_COST, group="job",
)
async def export_video(ctx: Ctx, format: Optional[str] = None):
    from app.services import export_service
    state = ctx.state
    # 只覆盖这一次导出的字幕方式；tracks/default_track/style 仍取项目保存的方案，
    # 其中 style 由 resolve_plan 从 state.subtitle_style 合入（样式面板改的才是准的）。
    plan = (export_service.resolve_plan(state, {"format": format, "enabled": format != "off"})
            if format is not None else None)
    if not export_service.is_available():
        raise CommandError("服务器上没有 ffmpeg，无法导出")
    needs = status.needs(state)
    if state.enable_dubbing and needs["dub"]:
        raise CommandError(f"还有 {needs['dub']} 句配音没做好/已过期，先 redub")
    if needs["translate"]:
        raise CommandError(f"还有 {needs['translate']} 句译文没做好/已过期，先 retranslate")

    async def body(job):
        async def relay(event):
            await ctx.emit({**event, "job": job.id})
        return await export_service.export_video(ctx.video_id, emit=relay, plan=plan)

    job = await jobs.run(ctx.video_id, "export", "导出", ctx.actor, body, exclusive=True)
    result = await job.task
    if job.error:
        raise CommandError(f"导出失败：{job.error}")
    if result is None:
        raise CommandError("导出没有完成（可能被取消）")
    ctx.ui("export_ready", url=f"/api/projects/{ctx.video_id}/export/download")
    return {"container": result.container, "size_mb": round(__import__("os").path.getsize(result.path) / 1048576, 1),
            "download": f"/api/projects/{ctx.video_id}/export/download"}


# ----------------------------------------------------------------------------
# 界面联动：AI 让界面做某件事（不改项目状态）
# ----------------------------------------------------------------------------

@command(
    "focus_line", "让界面滚动并定位到某一句，可选地从那里开始播放原视频。用来向用户展示你在说哪一句。",
    {"type": "object", "properties": {
        "line": {"type": "integer", "minimum": 1}, "play": {"type": "boolean"},
    }, "required": ["line"]},
    readonly=True, group="ui",
)
def focus_line(ctx: Ctx, line: int, play: bool = False):
    i, seg = resolve_line(ctx.state, line)
    ctx.ui("focus_line", line=i + 1, id=seg.id, start=seg.start_time, play=bool(play))
    return {"line": i + 1}


@command(
    "open_panel", "打开界面上的某个面板。",
    {"type": "object", "properties": {
        "panel": {"type": "string", "enum": ["script", "subtitle_style", "export", "settings"]},
    }, "required": ["panel"]},
    readonly=True, group="ui",
)
def open_panel(ctx: Ctx, panel: str):
    ctx.ui("open_panel", panel=panel)
    return {"opened": panel}


@command(
    "request_browser_action",
    "有些事只能在用户的浏览器里做：人声分离（需要用户的 GPU/WASM）、字幕烧录。用这个请用户触发。",
    {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["separate_vocals", "burn_subtitles"]},
        "reason": {"type": "string", "maxLength": 200},
    }, "required": ["action"]},
    readonly=True, group="ui",
)
def request_browser_action(ctx: Ctx, action: str, reason: str = ""):
    ctx.ui("browser_action", action=action, reason=reason)
    return {"requested": action, "note": "已在界面上提示用户，需要用户点确认；结果会出现在最近改动里"}
