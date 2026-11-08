"""字幕样式命令：改字号/颜色/位置/轨道。样式是项目的一部分，AI 和面板改的是同一份。"""
from typing import Optional

from app.commands.registry import CommandError, Ctx, command
from app.services import subtitle_service
from app.services.subtitle_service import SubtitleStyle

_FIELDS = {
    "track": {"type": "string", "enum": list(SubtitleStyle.VALID_TRACKS), "description": "显示哪种文字：译文/原文/双语"},
    "font_size_percent": {"type": "number", "minimum": 1.5, "maximum": 12.0, "description": "字号，占视频高度的百分比"},
    "primary_color": {"type": "string", "description": "文字颜色，如 #FFFFFF"},
    "outline_color": {"type": "string", "description": "描边颜色"},
    "outline_width": {"type": "number", "minimum": 0, "maximum": 8},
    "shadow": {"type": "number", "minimum": 0, "maximum": 8},
    "bold": {"type": "boolean"},
    "background": {"type": "string", "enum": list(SubtitleStyle.VALID_BACKGROUNDS), "description": "none 无底；box 半透明底框"},
    "alignment": {"type": "string", "enum": list(SubtitleStyle.VALID_ALIGNMENTS)},
    "margin_v_percent": {"type": "number", "minimum": 0, "maximum": 40, "description": "上下边距，占视频高度百分比"},
    "margin_h_percent": {"type": "number", "minimum": 0, "maximum": 40},
    "max_chars_per_line": {"type": "integer", "minimum": 6, "maximum": 60},
    "max_lines": {"type": "integer", "minimum": 1, "maximum": 4},
}


@command(
    "get_subtitle_style", "看当前的字幕样式和可调项。", readonly=True, group="read",
)
def get_subtitle_style(ctx: Ctx):
    state = ctx.state
    current = SubtitleStyle.from_dict(state.subtitle_style or None).to_dict()
    return {"style": current, "defaults": subtitle_service.default_style().to_dict()}


@command(
    "set_subtitle_style", "改字幕样式（只传要改的项，其余保持不变）。立刻在预览里生效。",
    {"type": "object", "properties": _FIELDS},
    group="style",
)
def set_subtitle_style(ctx: Ctx, **patch):
    if not patch:
        raise CommandError("至少给一项要改的样式")
    state = ctx.state
    merged = SubtitleStyle.from_dict({**SubtitleStyle.from_dict(state.subtitle_style or None).to_dict(), **patch})

    def apply(s):
        s.subtitle_style = merged.to_dict()
    ctx.change(apply, "改字幕样式：" + "、".join(patch))
    return {"style": merged.to_dict()}


@command(
    "reset_subtitle_style", "字幕样式恢复默认。", group="style",
)
def reset_subtitle_style(ctx: Ctx):
    def apply(s):
        s.subtitle_style = {}
    ctx.change(apply, "字幕样式恢复默认")
    return {"style": subtitle_service.default_style().to_dict()}


@command(
    "set_export_plan", "设置导出时怎么带字幕：不带 / 外挂软字幕(mp4) / 内嵌带样式(mkv) / 烧录(需要浏览器)。"
    "也可选要哪些轨道。",
    {"type": "object", "properties": {
        "format": {"type": "string", "enum": ["off", "soft", "styled", "burn"]},
        "tracks": {"type": "array", "items": {"type": "string", "enum": list(SubtitleStyle.VALID_TRACKS)}},
        "default_track": {"type": "string", "enum": list(SubtitleStyle.VALID_TRACKS)},
    }},
    group="style",
)
def set_export_plan(ctx: Ctx, format: Optional[str] = None, tracks: Optional[list] = None,
                    default_track: Optional[str] = None):
    if format is None and tracks is None and default_track is None:
        raise CommandError("至少给 format / tracks / default_track 之一")
    state = ctx.state
    current = subtitle_service.ExportPlan.from_dict(state.subtitle_export or None).to_dict()
    if format is not None:
        current["format"] = format
        current["enabled"] = format != "off"
    if tracks is not None:
        current["tracks"] = tracks
    if default_track is not None:
        current["default_track"] = default_track
    plan = subtitle_service.ExportPlan.from_dict(current).to_dict()
    plan.pop("style", None)                    # 样式只有一份：subtitle_style

    def apply(s):
        s.subtitle_export = plan
    ctx.change(apply, "改导出字幕方案")
    return {"plan": plan}
