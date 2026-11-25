"""
字幕样式预设：业界规范的数字翻译成我们的字段。

来源：
  · Netflix TTSG (Timed Text Style Guide)：成人节目 ≤42 拉丁字符/行（≈21 CJK 格），
    2 行，最短 5/6 秒（≈0.833s），最长 7 秒，阅读速度 ≤17 cps（拉丁）≈ 8.5 CJK 格/秒
  · YouTube（自动字幕 + 创作者惯例）：更紧凑，无背景框，粗描边，字号略大
  · 剪映专业版（热门"综艺字幕"风格）：大字号、粗描边、底部高置、半透明底框
  · BBC / 广播惯例：两行、底对齐、阅读速度 ≤160–180 wpm

每个预设给**完整的** SubtitleStyle 字段；缺失的走 default_style()。
预设不是"样式模板"的展示，是"一键到合规"：用户选完仍能微调。
"""
from __future__ import annotations

from app.services.subtitle_service import SubtitleStyle

PRESETS: dict[str, dict] = {
    # 默认：当前项目的风格（和 default_style 一致，仅作占位，方便前端列出来）
    "default": {
        "label": "默认",
        "description": "项目默认：半透明底框、粗体、底部",
        "style": {},
    },
    "netflix": {
        "label": "Netflix",
        "description": "TTSG 规范：2 行、每行 21 字、白字黑边、无底框、7 秒上限",
        "style": {
            "font_family": "Source Han Sans",
            "font_size_percent": 4.2,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 2.2,
            "shadow": 0.0,
            "bold": False,
            "italic": False,
            "blur": 0.0,
            "spacing": 0.0,
            "background": "none",
            "alignment": "bottom",
            "margin_v_percent": 6.0,
            "max_chars_per_line": 21,     # 42 拉丁字符
            "max_lines": 2,
            "min_duration": 5 / 6,        # 0.833s
            "max_duration": 7.0,
            "max_cps": 8.5,               # 17 拉丁 cps
            "min_gap": 0.042,
            "extend_into_silence": 0.5,
        },
    },
    "youtube": {
        "label": "YouTube",
        "description": "创作者风格：无底框、粗黑边、字稍大、节奏紧凑",
        "style": {
            "font_size_percent": 4.8,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 3.0,
            "shadow": 1.0,
            "bold": True,
            "background": "none",
            "alignment": "bottom",
            "margin_v_percent": 7.0,
            "max_chars_per_line": 20,
            "max_lines": 2,
            "min_duration": 0.8,
            "max_duration": 6.0,
            "max_cps": 9.5,
            "extend_into_silence": 0.3,
        },
    },
    "variety": {
        "label": "剪映综艺",
        "description": "综艺花字：大字、粗边、半透明底框、位置偏高",
        "style": {
            "font_family": "Source Han Sans",
            "font_size_percent": 5.6,
            "primary_color": "#FFFFFF",
            "outline_color": "#1A1A1A",
            "outline_width": 3.5,
            "shadow": 1.5,
            "bold": True,
            "background": "box",
            "alignment": "bottom",
            "margin_v_percent": 9.0,
            "max_chars_per_line": 16,
            "max_lines": 2,
            "min_duration": 0.8,
            "max_duration": 5.0,
            "max_cps": 9.0,
            "extend_into_silence": 0.4,
        },
    },
    "broadcast": {
        "label": "广播/BBC",
        "description": "传统播出：底框、正体、阅读速度保守、时间充裕",
        "style": {
            "font_family": "Source Han Sans",
            "font_size_percent": 4.0,
            "primary_color": "#FFFFFF",
            "outline_color": "#000000",
            "outline_width": 1.8,
            "shadow": 0.0,
            "bold": False,
            "background": "box",
            "alignment": "bottom",
            "margin_v_percent": 5.5,
            "max_chars_per_line": 19,
            "max_lines": 2,
            "min_duration": 1.0,
            "max_duration": 7.0,
            "max_cps": 7.5,               # ≈ 160 wpm
            "extend_into_silence": 0.6,
        },
    },
}


def preset_ids() -> list[str]:
    return list(PRESETS)


def preset_style(preset_id: str, base: SubtitleStyle | None = None) -> SubtitleStyle:
    """把预设叠在 base 上（base 缺省用 default_style），返回归一化后的样式。"""
    if preset_id not in PRESETS:
        raise ValueError(f"unknown subtitle preset: {preset_id!r}")
    base = base or SubtitleStyle()
    patch = PRESETS[preset_id]["style"]
    merged = SubtitleStyle.from_dict({**base.to_dict(), **patch})
    # 预设不动 track（用户选哪种字幕轨道和样式无关）
    merged.track = base.track
    return merged.normalised()


def presets_public() -> list[dict]:
    """给前端的清单：id + label + description + 完整 style（前端预览直接用）。"""
    return [
        {"id": pid, "label": p["label"], "description": p["description"],
         "style": preset_style(pid).to_dict()}
        for pid, p in PRESETS.items()
    ]
