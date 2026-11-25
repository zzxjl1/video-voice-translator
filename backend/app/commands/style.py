"""字幕样式命令：改字号/颜色/位置/轨道。样式是项目的一部分，AI 和面板改的是同一份。"""
from typing import Optional

from app.commands.registry import CommandError, Ctx, command
from app.services import subtitle_presets, subtitle_service
from app.services.subtitle_service import SubtitleStyle

_FIELDS = {
    "track": {"type": "string", "enum": list(SubtitleStyle.VALID_TRACKS), "description": "显示哪种文字：译文/原文/双语"},
    "font_size_percent": {"type": "number", "minimum": 1.5, "maximum": 12.0, "description": "字号，占视频高度的百分比"},
    "primary_color": {"type": "string", "description": "文字颜色，如 #FFFFFF"},
    "outline_color": {"type": "string", "description": "描边颜色"},
    "outline_width": {"type": "number", "minimum": 0, "maximum": 8},
    "shadow": {"type": "number", "minimum": 0, "maximum": 8},
    "bold": {"type": "boolean"},
    "italic": {"type": "boolean", "description": "斜体"},
    "secondary_color": {"type": "string", "description": "双语轨道里原文的颜色（单轨时无效果）"},
    "blur": {"type": "number", "minimum": 0, "maximum": 8, "description": "边缘模糊，代替硬描边做柔和边缘；0 = 不用"},
    "spacing": {"type": "number", "minimum": -5, "maximum": 20, "description": "字间距（像素）"},
    "background": {"type": "string", "enum": list(SubtitleStyle.VALID_BACKGROUNDS), "description": "none 无底；box 半透明底框"},
    "alignment": {"type": "string", "enum": list(SubtitleStyle.VALID_ALIGNMENTS)},
    "margin_v_percent": {"type": "number", "minimum": 0, "maximum": 40, "description": "上下边距，占视频高度百分比"},
    "margin_h_percent": {"type": "number", "minimum": 0, "maximum": 40},
    "max_chars_per_line": {"type": "integer", "minimum": 6, "maximum": 60},
    "max_lines": {"type": "integer", "minimum": 1, "maximum": 4},
    # 时间轴（专业字幕规则）
    "min_duration": {"type": "number", "minimum": 0.1, "maximum": 10, "description": "一条字幕最短显示秒数"},
    "max_duration": {"type": "number", "minimum": 1.5, "maximum": 30, "description": "一条字幕最长秒数，超过按行切成连续几条"},
    "max_cps": {"type": "number", "minimum": 3, "maximum": 30, "description": "阅读速度上限（汉字/秒；拉丁字符算半个）。超了会切分"},
    "min_gap": {"type": "number", "minimum": 0, "maximum": 0.5, "description": "相邻两条字幕之间的最小间隔秒数，让切换看得出来"},
    "extend_into_silence": {"type": "number", "minimum": 0, "maximum": 3, "description": "说完后字幕往后面的静音延伸多少秒再消失"},
    "fps": {"type": "number", "minimum": 0, "maximum": 240, "description": "字幕起止对齐到帧边界（23.976/25/30/60）；0 = 不对齐"},
}


@command(
    "get_subtitle_style", "看当前的字幕样式、可调项和可用预设。", readonly=True, group="read",
)
def get_subtitle_style(ctx: Ctx):
    state = ctx.state
    current = SubtitleStyle.from_dict(state.subtitle_style or None).to_dict()
    return {"style": current, "defaults": subtitle_service.default_style().to_dict(),
            "presets": [{"id": p["id"], "label": p["label"], "description": p["description"]} for p in subtitle_presets.presets_public()]}


@command(
    "apply_subtitle_preset", "一键套用业界字幕规范预设，然后还能用 set_subtitle_style 微调。",
    {"type": "object", "properties": {
        "preset": {"type": "string", "enum": subtitle_presets.preset_ids(),
                   "description": "netflix（TTSG 规范）/ youtube / variety（剪映综艺）/ broadcast（BBC）/ default（恢复默认）"},
    }, "required": ["preset"]},
    group="style",
)
def apply_subtitle_preset(ctx: Ctx, preset: str):
    try:
        merged = subtitle_presets.preset_style(preset, SubtitleStyle.from_dict(ctx.state.subtitle_style or None))
    except ValueError as e:
        raise CommandError(str(e)) from e

    def apply(s):
        s.subtitle_style = merged.to_dict()
    label = subtitle_presets.PRESETS[preset]["label"]
    ctx.change(apply, f"套用字幕预设：{label}")
    return {"style": merged.to_dict(), "preset": preset}


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
    "set_export_plan", "设置导出时怎么带字幕：不带 / 外挂软字幕(mp4) / 内嵌带样式(mkv)。"
    "也可选要哪些轨道。烧录字幕只能在界面的导出弹窗里做，不能设成项目方案。",
    {"type": "object", "properties": {
        "format": {"type": "string", "enum": ["off", "soft", "styled"]},
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
