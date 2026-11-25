"""
Subtitle generation: timeline adaptation, SRT and ASS rendering.

One style definition, three consumers
-------------------------------------
`SubtitleStyle` is shared by:

  * the in-app preview overlay (the frontend mirrors the same numbers, so what
    you see over the player is what gets exported),
  * the subtitle tracks embedded in the exported file (MP4 `mov_text` or MKV
    with full ASS styling),
  * browser-side burn-in, which draws with Canvas2D using the same values.

Why sizes are percentages, never pixels
---------------------------------------
Font size and margins are stored as a PERCENTAGE OF THE VIDEO HEIGHT and
resolved against the real frame size at render time. Absolute pixels cannot
work here: the same style has to look identical on a 720p and a 1080p export,
and the preview overlay lives in an element whose CSS size is unrelated to the
video's real resolution. Percentages satisfy all three consumers at once. This
is the core of "the subtitles have to adapt".

Why the timeline is rebuilt rather than reused
----------------------------------------------
`Segment.start_time`/`end_time` come from ASR and describe the ORIGINAL speech.
The dubbed audio is a different length: speech-rate fitting deliberately
stretches or squeezes each line to fill its slot, and a line can still overrun.
Using the ASR times verbatim makes subtitles vanish while the dub is still
talking. `build_cues` therefore extends each cue to cover the audio that
actually exists, then clamps it so two cues never overlap on screen.

Nothing here needs libass, freetype or a font on the server: SRT and ASS are
text formats, and only *burning* needs a renderer (which is why burn-in runs in
the browser instead — see the export notes).
"""
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, field
from typing import Iterable, Optional

from app import config
from app.services import audio_probe

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Style
# --------------------------------------------------------------------------


@dataclass
class SubtitleStyle:
    """
    How subtitles look, independent of video resolution.

    Every size-ish field is a percentage so the same style is resolution
    agnostic. `font_family` is a PLAYER-SIDE font name: nothing is rendered on
    the server, so the server never needs the font installed.
    """

    # Which text goes on screen.
    track: str = "translated"           # translated | original | bilingual

    font_family: str = "Source Han Sans"
    font_size_percent: float = 4.5      # of video height
    primary_color: str = "#FFFFFF"
    secondary_color: str = "#FFFFFF"    # ASS SecondaryColour：卡拉OK/说话人变色用；普通字幕同主色
    outline_color: str = "#000000"
    outline_width: float = 2.0
    shadow: float = 1.0
    bold: bool = True
    italic: bool = False
    blur: float = 0.0                   # 模糊（代替硬描边做柔和边缘），千分比高度
    spacing: float = 0.0                # 字间距，像素 @ PlayRes
    background: str = "box"             # none | box

    alignment: str = "bottom"           # bottom | center | top
    margin_v_percent: float = 6.0
    margin_h_percent: float = 5.0

    # Line breaking. CJK has no spaces, so wrapping is by display width.
    max_chars_per_line: int = 18
    max_lines: int = 2

    # ---- 时间轴（专业字幕规则）----
    # Never flash a cue for less than this.
    min_duration: float = 0.8
    # Never hold a single cue longer than this (viewers re-read and lose track);
    # a cue that would exceed it is split in time.
    max_duration: float = 7.0
    # Reading speed ceiling: display cells per second (CJK cells; a Latin char
    # is half a cell). Above it the cue is split if possible. Netflix: ~17 Latin
    # cps for adult programmes ≈ 8.5 cells; BBC: ~160–180 wpm. 9 cells is the
    # common ceiling for translated subtitles.
    max_cps: float = 9.0
    # Two consecutive cues never touch: keep at least this gap so the switch is
    # visible as a cut, not a flicker.
    min_gap: float = 0.042              # ≈ 2 frames @ 48fps; ~1 frame @ 24fps
    # Extend a cue past its audio end into the following silence so it doesn't
    # vanish the instant speech stops. Capped by the next cue and by max_duration.
    extend_into_silence: float = 0.5
    # Snap cue boundaries to frame boundaries (0 = off). Avoids a cue switching
    # mid-frame and flickering between two frames in some players.
    fps: float = 0.0                    # 0 = don't snap; 23.976/24/25/30/60 = snap
    # Keep this many frames of the previous cue visible after the next starts
    # when they're back-to-back within the same shot (0 = hard cut).
    # (Not used yet; reserved for a fade-in/out option.)

    VALID_TRACKS = ("translated", "original", "bilingual")
    VALID_ALIGNMENTS = ("bottom", "center", "top")
    VALID_BACKGROUNDS = ("none", "box")

    def normalised(self) -> "SubtitleStyle":
        """
        Clamp anything a hand-edited request could have got wrong.

        Every numeric field goes through `_clamped`, which falls back to the
        field's default for junk (`"abc"`, `None`, NaN) instead of raising — a
        style round-trips through the browser, and one bad value must not be
        able to 500 an export or the preview.
        """
        defaults = SubtitleStyle()
        out = SubtitleStyle(
            track=self.track if self.track in self.VALID_TRACKS else "translated",
            font_family=str(self.font_family or "sans-serif").strip() or "sans-serif",
            font_size_percent=_clamped(self.font_size_percent, defaults.font_size_percent, 1.5, 12.0),
            primary_color=_normalise_color(self.primary_color, "#FFFFFF"),
            secondary_color=_normalise_color(self.secondary_color, "#FFFFFF"),
            outline_color=_normalise_color(self.outline_color, "#000000"),
            outline_width=_clamped(self.outline_width, defaults.outline_width, 0.0, 8.0),
            shadow=_clamped(self.shadow, defaults.shadow, 0.0, 8.0),
            bold=_as_bool(self.bold, defaults.bold),
            italic=_as_bool(self.italic, defaults.italic),
            blur=_clamped(self.blur, defaults.blur, 0.0, 8.0),
            spacing=_clamped(self.spacing, defaults.spacing, -5.0, 20.0),
            background=(
                self.background if self.background in self.VALID_BACKGROUNDS else "box"
            ),
            alignment=(
                self.alignment
                if self.alignment in self.VALID_ALIGNMENTS
                else "bottom"
            ),
            margin_v_percent=_clamped(self.margin_v_percent, defaults.margin_v_percent, 0.0, 40.0),
            margin_h_percent=_clamped(self.margin_h_percent, defaults.margin_h_percent, 0.0, 40.0),
            max_chars_per_line=int(_clamped(self.max_chars_per_line, defaults.max_chars_per_line, 6, 60)),
            max_lines=int(_clamped(self.max_lines, defaults.max_lines, 1, 4)),
            min_duration=_clamped(self.min_duration, defaults.min_duration, 0.1, 10.0),
            max_duration=_clamped(self.max_duration, defaults.max_duration, 1.5, 30.0),
            max_cps=_clamped(self.max_cps, defaults.max_cps, 3.0, 30.0),
            min_gap=_clamped(self.min_gap, defaults.min_gap, 0.0, 0.5),
            extend_into_silence=_clamped(self.extend_into_silence, defaults.extend_into_silence, 0.0, 3.0),
            fps=_clamped(self.fps, defaults.fps, 0.0, 240.0),
        )
        # max_duration 必须大于 min_duration，否则一条字幕永远同时违反两条规则
        if out.max_duration < out.min_duration * 1.5:
            out.max_duration = out.min_duration * 1.5
        return out

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SubtitleStyle":
        """
        Build from a partial dict, ignoring unknown keys.

        Tolerant on purpose: a style is round-tripped through the browser, so a
        stale client must not be able to 500 an export with an extra field.
        """
        if not data:
            return cls().normalised()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known}).normalised()


def _clamped(value, default: float, lo: float, hi: float) -> float:
    """`value` as a float clamped to [lo, hi]; `default` when it is not a usable number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return min(hi, max(lo, number))


def _as_bool(value, default: bool) -> bool:
    """bool(...) would read the string "false" as True; JSON from a client may send it."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            return True
        if lowered in ("false", "0", "no", "off"):
            return False
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def default_style() -> SubtitleStyle:
    """The style a video starts with, from config."""
    return SubtitleStyle(
        track=config.SUBTITLE_DEFAULT_TRACK,
        font_family=config.SUBTITLE_FONT_FAMILY,
        font_size_percent=config.SUBTITLE_FONT_SIZE_PERCENT,
        margin_v_percent=config.SUBTITLE_MARGIN_V_PERCENT,
        margin_h_percent=config.SUBTITLE_MARGIN_H_PERCENT,
        max_chars_per_line=config.SUBTITLE_MAX_CHARS_PER_LINE,
    ).normalised()


# --------------------------------------------------------------------------
# Export plan
# --------------------------------------------------------------------------

# ISO 639-2/B tags. Players read these to auto-select a track for the viewer's
# locale, so an untagged track gets ignored on machines set to another language.
_LANGUAGE_TAGS = {
    "chinese": "chi", "english": "eng", "japanese": "jpn", "korean": "kor",
    "french": "fra", "german": "deu", "spanish": "spa", "portuguese": "por",
    "italian": "ita", "vietnamese": "vie", "indonesian": "ind", "thai": "tha",
    "russian": "rus", "arabic": "ara", "turkish": "tur", "hindi": "hin",
}

TRACK_TITLES = {
    "translated": "Translation",
    "original": "Original",
    "bilingual": "Bilingual",
}

# "burn" is accepted here so a plan round-tripped from the UI validates; the
# server reports it unavailable through `capabilities()` and never attempts it.
VALID_EXPORT_FORMATS = ("off", "soft", "styled", "burn")


def language_tag(name: Optional[str]) -> str:
    """ISO 639-2/B tag for a language name, or "und" when unknown."""
    return _LANGUAGE_TAGS.get((name or "").strip().lower(), "und")


@dataclass
class ExportPlan:
    """
    How subtitles are delivered in the exported file.

    Deliberately separate from `SubtitleStyle`: the style is what the text looks
    like, the plan is which tracks exist and in what container. A user can want
    styled subtitles in a soft MP4 track (where the styling is simply dropped)
    without changing their style.
    """

    enabled: bool = True
    format: str = "soft"                       # off | soft | styled | burn
    tracks: list[str] = field(default_factory=lambda: ["translated"])
    default_track: str = "translated"
    style: SubtitleStyle = field(default_factory=lambda: SubtitleStyle())

    def normalised(self) -> "ExportPlan":
        tracks = [
            t for t in (self.tracks or []) if t in SubtitleStyle.VALID_TRACKS
        ]
        if not tracks:
            tracks = ["translated"]
        # De-duplicate while keeping the order the user chose.
        seen: set[str] = set()
        tracks = [t for t in tracks if not (t in seen or seen.add(t))]

        default = self.default_track if self.default_track in tracks else tracks[0]

        return ExportPlan(
            enabled=bool(self.enabled),
            format=self.format if self.format in VALID_EXPORT_FORMATS else "off",
            tracks=tracks,
            default_track=default,
            style=self.style.normalised()
            if isinstance(self.style, SubtitleStyle)
            else SubtitleStyle.from_dict(self.style if isinstance(self.style, dict) else None),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "format": self.format,
            "tracks": list(self.tracks),
            "default_track": self.default_track,
            "style": self.style.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "ExportPlan":
        if not data:
            # No saved preference means "whatever the server is configured to
            # do", not "the dataclass defaults" — those are only a last resort.
            return default_export_plan()
        raw_style = data.get("style")
        style = (
            raw_style
            if isinstance(raw_style, SubtitleStyle)
            else SubtitleStyle.from_dict(raw_style if isinstance(raw_style, dict) else None)
        )
        raw_tracks = data.get("tracks")
        return cls(
            enabled=data.get("enabled", True),
            format=str(data.get("format") or "off"),
            tracks=list(raw_tracks) if isinstance(raw_tracks, (list, tuple)) else ["translated"],
            default_track=str(data.get("default_track") or "translated"),
            style=style,
        ).normalised()


def default_export_plan() -> ExportPlan:
    """The plan a video starts with, from config."""
    return ExportPlan(
        # 默认开启；能力差异（容器是否支持多轨、烧字幕只有浏览器能做等）由
        # 导出计划的能力位逐项如实上报，不设全局闸门。
        enabled=True,
        format=config.SUBTITLE_EXPORT_FORMAT,
        tracks=list(config.SUBTITLE_EXPORT_TRACKS) or ["translated"],
        default_track=(config.SUBTITLE_EXPORT_TRACKS or ["translated"])[0],
        style=default_style(),
    ).normalised()


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _normalise_color(value: str, fallback: str) -> str:
    match = _HEX_RE.match((value or "").strip())
    return f"#{match.group(1).upper()}" if match else fallback


# --------------------------------------------------------------------------
# Cues
# --------------------------------------------------------------------------


from app.services.subtitle_wrap import _display_width as _dw, add_custom_words, wrap_text  # noqa: E402


@dataclass
class Cue:
    start: float
    end: float
    text: str
    secondary: str = ""      # the other track, for bilingual
    # Pre-wrapped display lines, set by `_split_oversized` when a cue's text
    # needs more than `max_lines`. When present they ARE the display lines —
    # re-wrapping a re-joined chunk could shift breaks and drift from the
    # split that the timing was computed for.
    forced_lines: Optional[list[str]] = None
    # 双语：原文的显示行（和 forced_lines 配对，切分后的碎片靠它知道哪几行是原文）
    secondary_lines: Optional[list[str]] = None
    # 同一个源 cue 被切成的时间碎片共用一个 id：它们之间**不加 gap、不延伸**，
    # 必须无缝衔接（一句歌词/一句长译文被切开，中间不该闪一下空白）。
    group: Optional[str] = None

    def primary_lines(self, style: SubtitleStyle) -> list[str]:
        """主文案（译文）的显示行。forced_lines 时 = forced_lines 去掉原文行。"""
        if self.forced_lines is not None:
            sec = self.secondary_lines or []
            return [l for l in self.forced_lines if l not in sec] if sec else list(self.forced_lines)
        return _wrap(self.text, style.max_chars_per_line) if self.text else []

    def secondary_display_lines(self, style: SubtitleStyle) -> list[str]:
        """原文的显示行（只在双语轨道下有）。"""
        if self.forced_lines is not None:
            return list(self.secondary_lines or [])
        if style.track == "bilingual" and self.secondary:
            return _wrap(self.secondary, style.max_chars_per_line)
        return []

    def secondary_count(self, style: SubtitleStyle) -> int:
        """末尾这几行是原文——渲染端据此换成原文样式。"""
        return len(self.secondary_display_lines(style))

    def lines(self, style: SubtitleStyle) -> list[str]:
        """
        The wrapped display lines for this cue — ALL of them, never a cut:
        译文行在前，原文行在后。

        There is deliberately no `max_lines` clamp here. Cues that need more
        lines than the style allows are split in TIME by `_split_oversized`
        before they reach a renderer, so every cue that arrives here already
        fits; anything that somehow does not is rendered in full rather than
        truncated, because a subtitle that silently drops half a sentence is
        worse than one extra line.
        """
        if self.forced_lines is not None:
            return list(self.forced_lines)
        return self.primary_lines(style) + self.secondary_display_lines(style)


def _display_width(text: str) -> float:
    return _dw(text)


def _wrap(text: str, max_chars: int) -> list[str]:
    """断行（委托给 subtitle_wrap）：在标点/词边界断，两行平衡。"""
    return wrap_text(text, max_chars)


# `_limit_lines` used to live here: it kept the first `max_lines` lines and
# appended "…", silently dropping the rest of the sentence. It was removed on
# purpose — ALL text is shown, always. Oversized cues are split in time by
# `_split_oversized` so the line cap still shapes the layout, but no code path
# can make words disappear. Do not reintroduce a cut here.


# --------------------------------------------------------------------------
# Duration probing
# --------------------------------------------------------------------------

_duration_cache: dict[str, float] = {}


def probe_duration(path: str) -> Optional[float]:
    """
    Duration of an audio file in seconds, or None when it cannot be read.

    Cached: a video with a few hundred segments would otherwise shell out to
    ffprobe once per segment on every single export.

    MP3 (what TTS produces) is read straight from its frame headers — no
    process, ~0.2 ms. `audio_probe` answers only when it is certain; otherwise
    (Xing/VBRI header, damaged file, anything else) this falls through to ffprobe.
    """
    if not path:
        return None
    # 缓存 key 含 mtime 与大小：TTS 会**覆盖同一个路径**（tts/seg-x.mp3），
    # 只按路径做永久 key 的话，改了译文重合成之后，字幕时间轴用的还是旧时长
    # （P2 #24）。
    try:
        st = os.stat(path)
        key = f"{path}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return None
    cached = _duration_cache.get(key)
    if cached is not None:
        return cached

    if path.lower().endswith(".mp3"):
        direct = audio_probe.mp3_duration(path)
        if direct:
            _duration_cache[key] = direct
            return direct

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        duration = float(result.stdout.strip())
        if duration <= 0:
            return None
    except Exception as e:  # noqa: BLE001 - probing is best effort
        logger.debug("Could not probe duration of %s: %s", path, e)
        return None

    _duration_cache[key] = duration
    return duration


def probe_video_size(path: str) -> tuple[int, int]:
    """
    (width, height) of the first video stream, falling back to 1920x1080.

    Used as the ASS `PlayResX/PlayResY`, which is what makes percentage-based
    sizes resolve correctly for this particular video.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0:s=x",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        width, height = result.stdout.strip().split("x")[:2]
        width_i, height_i = int(width), int(height)
        if width_i > 0 and height_i > 0:
            return width_i, height_i
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not probe video size for %s: %s", path, e)
    return 1920, 1080


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


def _feed_custom_words(segments: Iterable, video_dir: str | None = None) -> None:
    """
    把专有名词登记给断行器：① 工程术语表的目标侧词；② 译文里反复出现的多字中文串
    （同一段文本里出现 ≥2 次、jieba 会切碎的）。否则"斯普利特河高中"会被拆成
    "斯普利特|河高中"。
    """
    words: set[str] = set()
    if video_dir:
        try:
            import json
            with open(os.path.join(video_dir, "llm", "glossary_extracted.json"), encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                words |= {str(v) for v in data.values() if v and "?" not in str(v)}
                words |= {str(k) for k in data.keys() if len(str(k)) >= 2}
        except (OSError, ValueError):
            pass
    # 译文里出现 ≥2 次的 2–5 字中文串（粗筛：没有标点/空格）
    from collections import Counter
    seen = Counter()
    for seg in segments:
        t = getattr(seg, "translated_text", "") or ""
        for m in re.finditer(r"[\u4e00-\u9fff]{2,5}", t):
            seen[m.group()] += 1
    # 只收"完整出现 ≥2 次"且不是另一个更长词的子串的
    cands = {w for w, c in seen.items() if c >= 2}
    words |= {w for w in cands if not any(w != o and w in o for o in cands)}
    if words:
        add_custom_words(words)


def build_cues(
    segments: Iterable,
    style: SubtitleStyle,
    *,
    probe_audio: bool = True,
    video_dir: str | None = None,
) -> list[Cue]:
    """
    Turn pipeline segments into display cues with adapted timing.

    Ordering is by start time (ASR output is normally ordered, but a recovered
    state file need not be). Each cue:

      * starts at the segment's original start,
      * lasts at least `min_duration`,
      * is extended to cover the ACTUAL dubbed audio when it is longer,
      * is clamped just before the next cue so two never overlap on screen —
        EXCEPT lines that genuinely overlap in time (two people talking at once):
        those become ONE two-line cue, one speaker per line (`_group_overlapping`).
    """
    # 隐藏的行不进字幕：预览叠加、SRT 下载、导出内嵌三条路都走这里，所以在这
    # 一处过滤就够。它的配音在时间轴上照常 —— 隐藏只管画面上的字。
    # 标成音乐/噪音/人声非语言的行同理不出字幕；歌曲（song）出原歌词，见 `_select_text`。
    segments = list(segments)
    _feed_custom_words(segments, video_dir)
    ordered = sorted(
        (s for s in segments
         if not getattr(s, "hidden", False)
         and getattr(s, "kind", "speech") in ("speech", "song")
         and ((getattr(s, "translated_text", "") or "").strip()
              or (getattr(s, "text", "") or "").strip())),
        key=lambda s: getattr(s, "start_time", 0.0) or 0.0,
    )

    cues: list[Cue] = []
    groups = _group_overlapping(ordered)
    for index, group in enumerate(groups):
        picked = [(seg, *_select_text(seg, style.track)) for seg in group]
        picked = [p for p in picked if p[1]]
        if not picked:
            continue

        start = max(0.0, min(float(getattr(seg, "start_time", 0.0) or 0.0) for seg, _, _ in picked))
        end = start + style.min_duration
        for seg, _, _ in picked:
            seg_start = max(0.0, float(getattr(seg, "start_time", 0.0) or 0.0))
            end = max(end, float(getattr(seg, "end_time", seg_start) or seg_start))
            # Cover the real dub. Speech-rate fitting can leave the audio longer
            # than the ASR slot, and a cue that ends early looks like a bug.
            if probe_audio:
                audio_duration = getattr(seg, "actual_duration", None)
                audio_path = getattr(seg, "audio_path", None)
                if not audio_duration and audio_path and os.path.exists(audio_path):
                    audio_duration = probe_duration(audio_path)
                if audio_duration:
                    end = max(end, seg_start + audio_duration)

        # Never overlap the next cue.
        if index + 1 < len(groups):
            next_start = min(float(getattr(g, "start_time", 0.0) or 0.0) for g in groups[index + 1])
            if next_start > start:
                end = min(end, max(start + 0.2, next_start - 0.04))

        if len(picked) == 1 and getattr(picked[0][0], "kind", "speech") == "song":
            cues.extend(_song_cues(_song_lyric(picked[0][0]), style, start, max(end, start + 0.2)))
        elif len(picked) == 1:
            _, primary, secondary = picked[0]
            cue = Cue(start=start, end=max(end, start + 0.2), text=primary, secondary=secondary)
            cues.extend(_split_oversized(cue, style))
        else:
            cues.append(_dual_cue(picked, style, start, max(end, start + 0.2)))

    return _apply_timing_rules(cues, style)


# --------------------------------------------------------------------------
# 时间轴后处理：专业字幕的时序规则，在所有 cue 都生成之后统一做一遍，
# 因为 gap / 延伸 / 帧对齐都要看相邻的 cue。
# --------------------------------------------------------------------------


def _snap(t: float, fps: float) -> float:
    """对齐到帧边界（向上取整到下一帧的起点，避免切在两帧之间）。"""
    if fps <= 0:
        return t
    return round(round(t * fps) / fps, 6)


def _cue_cells(cue: Cue, style: SubtitleStyle) -> float:
    """
    一条字幕的阅读量（CJK 格），用来算阅读速度。
    **只算主文案**：双语轨道里观众读的是译文，原文只是参考，不该因为原文长就把
    译文也切掉（一条 2.36s 的字幕，译文 14 格 = 5.9 cps 合规，加上英文原文就 18 cps）。
    """
    return sum(_display_width(l) for l in cue.primary_lines(style)) / 2.0


def _split_by_reading(cue: Cue, style: SubtitleStyle) -> list[Cue]:
    """
    一条字幕如果读不完（主文案超过 max_cps）或停太久（超过 max_duration），按主文案
    行切成连续的几条，每条的时间够读完。文本不丢。

    切块复用 `_split_oversized` 的逻辑（译文 ≤N 行 + 按比例配对的原文），这里只负责
    **重新分配时间**：每条至少够读完（cells / max_cps）且不短于 min_duration。
    """
    cells = _cue_cells(cue, style)
    duration = cue.end - cue.start
    too_fast = duration > 0 and cells / duration > style.max_cps
    too_long = duration > style.max_duration
    if not (too_fast or too_long):
        return [cue]
    if len(cue.primary_lines(style)) <= 1 and not (too_long and cue.secondary_display_lines(style)):
        # 一行主文案：没法再按行切（硬切会把词拆开），只能保留
        return [cue]
    pieces = _split_oversized(cue, style)
    if len(pieces) <= 1:
        return [cue]
    # 重新按阅读速度分配时间
    needs = [max(_cue_cells(p, style) / style.max_cps, style.min_duration) for p in pieces]
    total_need = sum(needs)
    if duration >= total_need:
        spare = duration - total_need
        weights = [max(_cue_cells(p, style), 1.0) for p in pieces]
        tw = sum(weights)
        durs = [n + spare * w / tw for n, w in zip(needs, weights)]
    else:
        # 总时长连"每条读满"都不够：按文字量硬分，保证不丢字（源时间轴的物理限制）
        weights = [max(_cue_cells(p, style), 1.0) for p in pieces]
        tw = sum(weights)
        durs = [duration * w / tw for w in weights]
    t = cue.start
    for p, d in zip(pieces, durs):
        p.start = t
        p.end = t + d
        t += d
    return pieces


def _apply_timing_rules(cues: list[Cue], style: SubtitleStyle) -> list[Cue]:
    """
    按顺序应用：
      1. 阅读速度 / 最大时长 → 切分（可能把一条变几条）
      2. 延伸进静音：一条字幕说完后，后面如果是空的，多停 extend_into_silence
      3. 最小时长
      4. 相邻不重叠且留 min_gap
      5. 帧对齐
    顺序有讲究：先切再延伸，否则切出来的碎片各自延伸会叠起来；gap 在延伸之后，
    否则延伸会把 gap 吃掉；帧对齐最后，避免取整后又产生重叠。
    """
    if not cues:
        return cues
    # 1. 切分
    out: list[Cue] = []
    for c in cues:
        out.extend(_split_by_reading(c, style))
    cues = out

    # 2. 延伸进静音 + 3. 最小时长 + 4. gap（一次从前往后扫）
    for i, c in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        same_group = nxt is not None and c.group is not None and nxt.group == c.group
        if same_group:
            # 同一源 cue 的碎片：无缝衔接，不延伸、不 gap（时长在切分时已分好）
            continue
        next_start = nxt.start if nxt else float("inf")
        # 自然结束点（音频结束）记下来，延伸从它算，不从被钳过的 end 算
        natural_end = c.end
        # 最小时长
        end = max(c.end, c.start + style.min_duration)
        # 延伸：只往空隙里伸，不超过下一条开始 - gap，也不超过 max_duration
        if style.extend_into_silence > 0:
            end = max(end, natural_end + style.extend_into_silence)
        end = min(end, c.start + style.max_duration)
        if next_start - style.min_gap > c.start:
            end = min(end, next_start - style.min_gap)
        else:
            # 下一条几乎同时开始：保证至少 0.2s 可见，哪怕会和下条紧贴
            end = max(c.start + 0.2, min(end, next_start))
        c.end = max(0.0, end)

    # 5. 帧对齐：起止各自取整到帧；取整后如果和下一条重叠，把 end 退一帧
    if style.fps > 0:
        fps = style.fps
        for c in cues:
            c.start = _snap(c.start, fps)
            c.end = _snap(c.end, fps)
            if c.end <= c.start:
                c.end = c.start + 1.0 / fps
        for a, b in zip(cues, cues[1:]):
            if a.group is not None and a.group == b.group:
                b.start = a.end            # 碎片共用边界，不留洞
            elif a.end > b.start:
                a.end = max(a.start + 1.0 / fps, b.start - 1.0 / fps)

    return cues


# 两句话的时间重叠超过这么多秒，就当作"同时在说"：合成一条两行的字幕。ASR 的边界本来就有几十毫秒的
# 误差，所以不能一有交叠就算。
OVERLAP_MERGE_S = 0.25


def _group_overlapping(ordered: list) -> list[list]:
    """
    按开始时间排好的行，把互相重叠的相邻几行分成一组。

    以前"字幕永不重叠"的做法是把前一条截到后一条开始之前 —— 两个人同时说话时，前一个人还在说，
    他的字幕已经没了。重叠的对话改成**一条**字幕、每人一行（业界对话字幕的惯例，在 SRT 的软字幕、
    样式化字幕、浏览器烧录里表现一致；真的叠两条同时显示，各播放器处理不一）。
    """
    groups: list[list] = []
    group_end = 0.0
    for seg in ordered:
        start = float(getattr(seg, "start_time", 0.0) or 0.0)
        end = float(getattr(seg, "end_time", start) or start)
        if groups and start < group_end - OVERLAP_MERGE_S:
            groups[-1].append(seg)
            group_end = max(group_end, end)
        else:
            groups.append([seg])
            group_end = end
    return groups


def _dual_cue(picked: list, style: SubtitleStyle, start: float, end: float) -> Cue:
    """每个说话的人一行，前面加 "- "。双语轨道时，每个人的另一语言跟在后面。"""
    lines: list[str] = []
    for _, primary, _ in picked:
        lines.extend(_wrap(f"- {primary}", style.max_chars_per_line))
    if style.track == "bilingual":
        for _, _, secondary in picked:
            if secondary:
                lines.extend(_wrap(f"- {secondary}", style.max_chars_per_line))
    # 不再按 max_lines 在时间上切开：切开就不是"同时"了。宁可多一行，不丢话。
    return Cue(start=start, end=end, text="", secondary="", forced_lines=lines)


def _split_oversized(cue: Cue, style: SubtitleStyle) -> list[Cue]:
    """
    A cue whose text needs more than `max_lines` lines is split into
    consecutive cues that each fit — the text is never dropped.

    双语轨道：译文和原文**各自**最多 `max_lines` 行（专业做法：双语字幕一块最多
    2×N 行，不是两种语言加起来 N 行）。超过时按"译文行数 / 原文行数"比例切成连续
    的几块，每块译文行在前、原文行在后，时间按文字量分配。

    The old behaviour kept the first `max_lines` lines and appended "…",
    silently discarding the rest of the sentence. Subtitles exist to carry the
    words; a line cap is a layout preference, not a licence to lose them.
    Chunks now play back-to-back, sharing the original duration in proportion
    to how much text each carries, so a long line simply stays on screen a
    little longer instead of vanishing.
    """
    primary = cue.primary_lines(style)
    secondary = cue.secondary_display_lines(style)
    # 两种语言各自的行数都对得上一个块 → 不用切
    if len(primary) <= style.max_lines and len(secondary) <= style.max_lines:
        return [cue]

    # 按"每种语言每次最多 max_lines 行"切块
    p_chunks = [primary[i:i + style.max_lines] for i in range(0, len(primary), style.max_lines)] or [[]]
    s_chunks = [secondary[i:i + style.max_lines] for i in range(0, len(secondary), style.max_lines)] or [[]]
    # 对齐：两种语言**都有**且块数不同时，按文字量把多的那种重新均分到少的块数里，
    # 让每块尽量是"译文 + 对应原文"。只有一种语言时直接用它自己的切块（每块 ≤ max_lines）。
    if secondary and len(p_chunks) != len(s_chunks):
        n = min(len(p_chunks), len(s_chunks))
        if len(p_chunks) > n:
            p_chunks = _even_chunks(primary, n)
        else:
            s_chunks = _even_chunks(secondary, n)
    elif not secondary:
        s_chunks = [[] for _ in p_chunks]
    elif not primary:
        p_chunks = [[] for _ in s_chunks]

    weights = [sum(_display_width(l) for l in p) + sum(_display_width(l) for l in s_) or 1.0
               for p, s_ in zip(p_chunks, s_chunks)]
    total = sum(weights)
    duration = max(0.2, cue.end - cue.start)

    out: list[Cue] = []
    t = cue.start
    gid = cue.group or f"g{id(cue):x}"
    for p, s_, w in zip(p_chunks, s_chunks, weights):
        d = duration * w / total
        out.append(Cue(start=t, end=t + d, text="", secondary="",
                       forced_lines=list(p) + list(s_),
                       secondary_lines=list(s_) or None, group=gid))
        t += d
    return out


def _even_chunks(lines: list[str], n: int) -> list[list[str]]:
    """把 lines 尽量均匀地分成 n 块（按显示宽度），每块至少一行（lines 非空时）。"""
    if n <= 0 or not lines:
        return [[] for _ in range(max(n, 0))]
    if len(lines) <= n:
        out = [[l] for l in lines] + [[] for _ in range(n - len(lines))]
        return out
    widths = [_display_width(l) for l in lines]
    total = sum(widths)
    out: list[list[str]] = []
    acc: list[str] = []
    acc_w = 0.0
    target = total / n
    for l, w in zip(lines, widths):
        acc.append(l); acc_w += w
        remaining_blocks = n - len(out) - 1
        if acc_w >= target - 1e-9 and remaining_blocks > 0 and len(lines) - (len(out) + sum(len(a) for a in out)) > 0:
            out.append(acc); acc = []; acc_w = 0.0
    if acc:
        out.append(acc)
    while len(out) < n:
        out.append([])
    return out[:n]


# 标签的形状规则。**必须与前端 `frontend/utils/subtitleStyle.ts` 的 TAG_SOURCE
# 逐字一致**：服务端是权威（覆盖层、烧录、SRT、ASS 都从 build_cues 出来），前端
# 那份是兜底。两边不一致会复现"屏幕上没有、下载的字幕文件里却有"这种半修状态。
#
#   · 半角 `[sad]` `[clears throat]`：配置里的规范写法（config.EMOTION_*_TAGS）
#   · 全角 `【讽刺】`：模型在中文提示下自作主张的写法
#   · 只匹配"像标签"的内容，`[1]` `[注]` 这类正文不会被误删
_TAG_RE = re.compile(
    r"""\[[A-Za-z][A-Za-z '\-]{0,23}\]|【[^】\n，。！？；：、,.!?;:]{1,8}】"""
)


def strip_subtitle_tags(text: str) -> str:
    """
    去掉字幕文本里的标签（[sad] / [gasp] / 【讽刺】…）。

    它们是多模态增强打给 TTS 的指令（控语气、加音效），不是给观众看的字。编辑器
    里必须保留（用户在那改标签），但**所有**字幕输出都要剥掉 —— 而覆盖层、烧录、
    SRT、ASS 全部经由 `build_cues` → `_select_text`，所以剥在这一处就够。

    顺带收拾留下的空格：行首标签去掉后不留前导空格，行中标签去掉后不出现双空格。
    """
    if not text or ("[" not in text and "【" not in text):
        return text
    collapsed = re.sub(r"[ \t]{2,}", " ", _TAG_RE.sub("", text))
    return collapsed.strip(" \t")


SONG_MARK = "♪"


def _song_lyric(seg) -> str:
    """歌曲行的歌词：有审听版（纠过错的）就用它，语气标签不出现在字幕里。歌词不翻译（译了也唱不出来）。"""
    return strip_subtitle_tags(((getattr(seg, "reviewed_text", "") or getattr(seg, "text", "") or "")).strip())


def _song_cues(lyric: str, style: SubtitleStyle, start: float, end: float) -> list[Cue]:
    """
    一句歌词 → 一条或几条字幕，每条的第一行行首、最后一行行尾各一个 ♪。

    先按"每行少两格"排版，给前后的 ♪ 留位置：把 ♪ 拼进文字再排版，歌词长的时候结尾那个 ♪ 会被挤到
    单独一行、再被按时间切成一条只有 ♪ 的字幕。歌词超过 `max_lines` 行就按字数比例切成连续的几条
    （和 `_split_oversized` 同一个做法：词不丢，只是在屏幕上多停一会儿）。
    """
    parts = _wrap(lyric, max(4, style.max_chars_per_line - 2))
    chunks = [parts[i:i + style.max_lines] for i in range(0, len(parts), style.max_lines)]
    weights = [sum(_display_width(l) for l in chunk) or 1.0 for chunk in chunks]
    total = sum(weights)
    duration = max(0.2, end - start)
    out: list[Cue] = []
    t = start
    gid = f"song{id(lyric):x}"
    for chunk, weight in zip(chunks, weights):
        lines = list(chunk)
        lines[0] = f"{SONG_MARK} {lines[0]}"
        lines[-1] = f"{lines[-1]} {SONG_MARK}"
        out.append(Cue(start=t, end=t + duration * weight / total, text="", forced_lines=lines, group=gid))
        t += duration * weight / total
    return out


def _select_text(seg, track: str) -> tuple[str, str]:
    if getattr(seg, "kind", "speech") == "song":
        # 所有轨道都是原歌词。这里是和别人重叠（合成两行字幕）时用的内联写法；单独一条的歌曲走 `_song_cues`。
        lyric = _song_lyric(seg)
        return (f"{SONG_MARK} {lyric} {SONG_MARK}" if lyric else ""), ""
    translated = strip_subtitle_tags((getattr(seg, "translated_text", "") or "").strip())
    original = strip_subtitle_tags((getattr(seg, "text", "") or "").strip())
    if track == "original":
        return original, ""
    if track == "bilingual":
        # Translation first: the viewer reads their language, the original is
        # there for reference.
        return translated or original, original if translated else ""
    return translated or original, ""


# --------------------------------------------------------------------------
# Renderers
# --------------------------------------------------------------------------


def _srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    whole = int(secs)
    millis = int(round((secs - whole) * 1000))
    if millis == 1000:      # rounding can land exactly on the next second
        whole += 1
        millis = 0
        if whole == 60:
            whole = 0
            minutes += 1
    # 秒的进位可能继续把分钟顶到 60（59:59.9996 → 60:00.000），分钟也要进位，
    # 否则会写出 "00:60:00" 这种非法时间戳（子代理报的可能，已核实）。
    if minutes == 60:
        minutes = 0
        hours += 1
    return f"{int(hours):02d}:{int(minutes):02d}:{whole:02d},{millis:03d}"


def to_srt(cues: list[Cue], style: SubtitleStyle) -> str:
    """SRT text. Line breaks are real newlines; SRT has no inline markup."""
    blocks: list[str] = []
    for index, cue in enumerate(cues, 1):
        body = "\n".join(cue.lines(style))
        if not body:
            continue
        blocks.append(
            f"{index}\n{_srt_time(cue.start)} --> {_srt_time(cue.end)}\n{body}\n"
        )
    return "\n".join(blocks)


def _ass_time(seconds: float) -> str:
    """
    `H:MM:SS.cc`. Rounded to centiseconds FIRST and split afterwards: formatting
    `secs` with `%05.2f` rounds up on its own, so 59.999 s used to print as the
    illegal `0:00:60.00`.
    """
    total_cs = max(0, int(round(max(0.0, seconds) * 100)))
    hours, rest = divmod(total_cs, 360000)
    minutes, rest = divmod(rest, 6000)
    return f"{hours:d}:{minutes:02d}:{rest // 100:02d}.{rest % 100:02d}"


def _ass_color(hex_color: str) -> str:
    """
    `#RRGGBB` -> ASS `&H00BBGGRR`.

    ASS stores colours as BGR with a leading alpha byte. Getting this backwards
    is the classic ASS mistake: red and blue swap, which is easy to miss on
    white-on-black text until someone sets a coloured outline.
    """
    value = _normalise_color(hex_color, "#FFFFFF").lstrip("#")
    red, green, blue = value[0:2], value[2:4], value[4:6]
    return f"&H00{blue}{green}{red}"


_ASS_ALIGNMENT = {"bottom": 2, "center": 5, "top": 8}


def _ass_number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


def _ass_field(text: str) -> str:
    """A value placed in a comma-separated ASS field: commas and line breaks would shift the columns."""
    return " ".join(str(text).replace(",", " ").split())


def to_ass(cues: list[Cue], style: SubtitleStyle, width: int, height: int) -> str:
    """
    ASS subtitle text, sized for THIS video.

    `PlayResX/PlayResY` are the real frame size and every size is derived from
    it, which is what makes one style work across resolutions. ASS is also the
    only way to carry real styling in a remux (`-c:v copy`) — MP4's `mov_text`
    supports no styling at all.

    Outline and shadow are PERCENT-OF-HEIGHT quantities (per mille, exactly as
    the preview's `resolveMetrics` computes them), converted to PlayRes pixels
    here. Passing the raw "2.0" through would mean 2 px at any resolution — a
    hairline at 1080p-relative sizing looks fine at 1080 and fat at 480.

    The translucent box is a SEPARATE style drawn one layer below the text. ASS
    only has one border colour per style, so a box (BorderStyle 3, which paints
    the box in OutlineColour) and a text outline cannot coexist in one style —
    but the preview shows both, so the export does too: the box line carries a
    fully transparent fill and draws only the box, the text line draws the
    outline and shadow on top.
    """
    font_size = max(8, round(height * style.font_size_percent / 100.0))
    margin_v = max(0, round(height * style.margin_v_percent / 100.0))
    margin_h = max(0, round(width * style.margin_h_percent / 100.0))
    outline = height * style.outline_width / 1000.0
    shadow = height * style.shadow / 1000.0
    box = style.background == "box"
    # Same padding the preview uses around a line (0.18 of the font size).
    box_pad = max(1.0, font_size * 0.18)

    # ASS 的 Fontname 是逗号分隔格式里的一个字段，**不能含逗号**，所以写不了字体栈。
    # 只写用户选的那一个名字；libass 找不到时按 fontconfig 自己的规则回退（通常到
    # 系统黑体），和前端 FONT_FALLBACKS 的目标一致但不受我们控制。这是 ASS 格式的
    # 限制，不是遗漏。
    font = _ass_field(style.font_family) or "sans-serif"
    alignment = str(_ASS_ALIGNMENT.get(style.alignment, 2))
    bold = "-1" if style.bold else "0"
    italic = "-1" if style.italic else "0"
    # 模糊：ASS 的 \blur 是每像素高斯半径；我们的 blur 千分比高度 → PlayRes 像素
    blur_px = height * style.blur / 1000.0

    def style_line(name, primary, secondary, outline_colour, back_colour, border_style,
                   outline_px, shadow_px, *, size=None, spacing=None, italic_flag=None, align=None):
        return ",".join(
            [
                f"Style: {name}",
                font,
                str(size if size is not None else font_size),
                primary,
                secondary,
                outline_colour,
                back_colour,
                bold,
                italic if italic_flag is None else italic_flag,
                "0", "0",
                "100", "100",
                _ass_number(style.spacing if spacing is None else spacing),
                "0",
                str(border_style),
                _ass_number(outline_px),
                _ass_number(shadow_px),
                alignment if align is None else str(align),
                str(margin_h), str(margin_h), str(margin_v),
                "1",
            ]
        )

    # 双语轨道：原文单独一个样式（小一号、灰一点），译文用 Default。
    # 同一个 Dialogue 里按行加 \r 样式切换：ASS 允许行内 \r<StyleName>。
    bilingual = style.track == "bilingual"
    secondary_size = max(8, round(font_size * 0.82))
    secondary_color = _ass_color(style.secondary_color) if style.secondary_color != "#FFFFFF" else "&H00D0D0D0"

    lines = [
        "[Script Info]",
        "; Generated by video-voice-translator.",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        # 2 = no automatic wrapping: `_wrap` already broke the lines, and
        # letting libass wrap too produces different breaks per player.
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding",
        style_line(
            "Default",
            _ass_color(style.primary_color),
            _ass_color(style.secondary_color),
            _ass_color(style.outline_color),
            "&H80000000",
            1,
            outline,
            shadow,
        ),
    ]
    if bilingual:
        lines.append(
            style_line(
                "Original",
                secondary_color,
                secondary_color,
                _ass_color(style.outline_color),
                "&H80000000",
                1,
                outline,
                shadow,
                size=secondary_size,
                italic_flag="0",
            )
        )
    if box:
        lines.append(
            style_line(
                "Box",
                "&HFF000000",      # fill fully transparent: only the box is drawn
                "&HFF000000",
                "&H80000000",      # BorderStyle 3 paints the box in OutlineColour; 50% black
                "&HFF000000",
                3,
                box_pad,
                0,
            )
        )
    lines += [
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # 行内覆盖标签：模糊（代替硬描边做柔和边缘）
    inline = ""
    if blur_px > 0.2:
        inline += f"\\blur{blur_px:.1f}"

    for cue in cues:
        # 双语：译文行 / 原文行分开，各自套样式；其它轨道一行样式到底
        if bilingual and cue.text and cue.secondary:
            primary_lines = _wrap(cue.text, style.max_chars_per_line)
            secondary_lines = _wrap(cue.secondary, style.max_chars_per_line)
            # 两个说话人重叠的 cue 走 forced_lines，不带 text/secondary
            if cue.forced_lines is not None:
                primary_lines, secondary_lines = cue.forced_lines, []
            pieces = []
            for t in primary_lines:
                pieces.append("{\\rDefault}" + inline + _ass_escape(t))
            for t in secondary_lines:
                pieces.append("{\\rOriginal}" + _ass_escape(t))
            text = "\\N".join(pieces)
        else:
            body = cue.lines(style)
            if not body:
                continue
            # \N is an explicit break, \n would be a soft one.
            text = "\\N".join(_ass_escape(l) for l in body)
            if inline:
                text = inline + text
        start, end = _ass_time(cue.start), _ass_time(cue.end)
        if box:
            lines.append(f"Dialogue: 0,{start},{end},Box,,0,0,0,,{text}")
        lines.append(f"Dialogue: 1,{start},{end},Default,,0,0,0,,{text}")

    return "\n".join(lines) + "\n"


def _ass_escape(text: str) -> str:
    """ASS 正文里 { } 是覆盖标签的边界，正文中出现要转义成全角括号。"""
    return text.replace("{", "(").replace("}", ")")


# --------------------------------------------------------------------------
# Capabilities
# --------------------------------------------------------------------------

_ffmpeg_filter_cache: Optional[set[str]] = None
_ffmpeg_encoder_cache: Optional[set[str]] = None


def _ffmpeg_listing(*args: str) -> set[str]:
    """Names from `ffmpeg -hide_banner -filters` / `-encoders` / `-muxers`."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return set()

    names: set[str] = set()
    for line in result.stdout.splitlines():
        # Rows look like: " T.. subtitles      V->V  Render text subtitles"
        parts = line.split()
        if len(parts) >= 2 and not line.startswith("Filters:") and not line.startswith("Encoders:"):
            names.add(parts[1] if parts[0].startswith(("-", "=")) is False else parts[0])
        # Also index every token so lookups are forgiving of column layout.
        names.update(parts)
    return names


def capabilities(force: bool = False) -> dict:
    """
    Which subtitle deliveries this server can perform, and why not.

    Mirrors `config.separation_capabilities()`: the UI greys out an option and
    shows the reason, instead of offering something that fails halfway.

    Note what is NOT here: burn-in. That needs libass/freetype plus an installed
    CJK font, and it forces a full video re-encode — which this class of host
    cannot afford. Burning therefore happens in the BROWSER (Canvas2D +
    MediaRecorder), and the client reports its own availability separately.
    """
    global _ffmpeg_filter_cache, _ffmpeg_encoder_cache

    if _ffmpeg_filter_cache is None or force:
        _ffmpeg_filter_cache = _ffmpeg_listing("-filters")
        _ffmpeg_encoder_cache = _ffmpeg_listing("-encoders")

    filters = _ffmpeg_filter_cache or set()
    encoders = _ffmpeg_encoder_cache or set()

    has_ffmpeg = bool(filters) and bool(encoders)
    can_mov_text = "mov_text" in encoders
    can_ass = "ass" in encoders
    can_subrip = "subrip" in encoders

    # `subtitles` is the burn-in filter; its absence is exactly why burn-in is
    # reported unavailable rather than attempted.
    burn_filters = {"subtitles", "ass", "drawtext"}
    has_burn_filter = bool(burn_filters & filters)

    return {
        "srt": {
            "available": True,
            "reason": None,
            "description": "Separate .srt file. Styling is the player's job.",
        },
        "soft": {
            "available": has_ffmpeg and can_mov_text,
            "reason": None
            if (has_ffmpeg and can_mov_text)
            else "this ffmpeg has no `mov_text` encoder",
            "description": "Subtitle tracks inside the MP4. Switchable and toggleable, no styling.",
        },
        "styled": {
            "available": has_ffmpeg and can_ass and can_subrip,
            "reason": None
            if (has_ffmpeg and can_ass and can_subrip)
            else "this ffmpeg cannot mux ASS subtitles",
            "description": "MKV with a fully styled ASS track. The video stream is still copied.",
        },
        # 键名是给前端的契约：改用 `burn`（前端 SubtitleDeliveryOption 里的名字）。
        # 以前叫 burn_server，而前端类型是 burn —— 于是 capabilities.burn 永远是
        # undefined，"能力探测"对这一个选项其实是失效的。
        "burn": {
            "available": has_ffmpeg and has_burn_filter,
            "reason": None
            if (has_ffmpeg and has_burn_filter)
            else (
                "this ffmpeg has no subtitles/ass/drawtext filter (it was built "
                "without --enable-libass --enable-libfreetype), so the server "
                "cannot render text into frames. Burn-in runs in the browser."
            ),
            "description": "Rendered into the picture. Requires re-encoding; cannot run here.",
        },
    }
