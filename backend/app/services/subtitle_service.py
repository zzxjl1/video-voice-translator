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
    outline_color: str = "#000000"
    outline_width: float = 2.0
    shadow: float = 1.0
    bold: bool = True
    background: str = "box"             # none | box

    alignment: str = "bottom"           # bottom | center | top
    margin_v_percent: float = 6.0
    margin_h_percent: float = 5.0

    # Line breaking. CJK has no spaces, so wrapping is by display width.
    max_chars_per_line: int = 18
    max_lines: int = 2

    # Never flash a cue for less than this.
    min_duration: float = 0.8

    VALID_TRACKS = ("translated", "original", "bilingual")
    VALID_ALIGNMENTS = ("bottom", "center", "top")
    VALID_BACKGROUNDS = ("none", "box")

    def normalised(self) -> "SubtitleStyle":
        """Clamp anything a hand-edited request could have got wrong."""
        return SubtitleStyle(
            track=self.track if self.track in self.VALID_TRACKS else "translated",
            font_family=(self.font_family or "sans-serif").strip(),
            font_size_percent=min(12.0, max(1.5, float(self.font_size_percent))),
            primary_color=_normalise_color(self.primary_color, "#FFFFFF"),
            outline_color=_normalise_color(self.outline_color, "#000000"),
            outline_width=min(8.0, max(0.0, float(self.outline_width))),
            shadow=min(8.0, max(0.0, float(self.shadow))),
            bold=bool(self.bold),
            background=(
                self.background if self.background in self.VALID_BACKGROUNDS else "box"
            ),
            alignment=(
                self.alignment
                if self.alignment in self.VALID_ALIGNMENTS
                else "bottom"
            ),
            margin_v_percent=min(40.0, max(0.0, float(self.margin_v_percent))),
            margin_h_percent=min(40.0, max(0.0, float(self.margin_h_percent))),
            max_chars_per_line=min(60, max(6, int(self.max_chars_per_line))),
            max_lines=min(4, max(1, int(self.max_lines))),
            min_duration=min(10.0, max(0.1, float(self.min_duration))),
        )

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
        enabled=config.SUBTITLE_EXPORT_ENABLED,
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


@dataclass
class Cue:
    start: float
    end: float
    text: str
    secondary: str = ""      # the other track, for bilingual

    def lines(self, style: SubtitleStyle) -> list[str]:
        """The wrapped display lines for this cue."""
        parts: list[str] = []
        if self.text:
            parts.extend(_wrap(self.text, style.max_chars_per_line))
        if style.track == "bilingual" and self.secondary:
            parts.extend(_wrap(self.secondary, style.max_chars_per_line))
        return _limit_lines(parts, style.max_lines)


def _display_width(text: str) -> float:
    """
    Rough display width in "character cells": CJK counts 1, Latin 0.5.

    Only used for line breaking, so approximate is fine — it just has to make
    Chinese lines break at roughly the same visual length as Latin ones.
    """
    width = 0.0
    for ch in text:
        width += 1.0 if _is_wide(ch) else 0.5
    return width


def _is_wide(ch: str) -> bool:
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFD
    )


def _wrap(text: str, max_chars: int) -> list[str]:
    """
    Break `text` into lines of at most `max_chars` display cells.

    Breaks on spaces when the text has them (Latin), otherwise per character
    (CJK). Prefers a space break near the limit so words are not split.
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    if _display_width(text) <= max_chars:
        return [text]

    has_spaces = " " in text
    lines: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if _display_width(candidate) > max_chars and current:
            # Try to move a trailing word to the next line when breaking a
            # space-delimited text, so Latin words stay whole.
            if has_spaces and ch != " ":
                cut = current.rfind(" ")
                if cut > 0 and _display_width(current[:cut]) >= max_chars * 0.5:
                    lines.append(current[:cut].rstrip())
                    current = current[cut + 1 :].lstrip() + ch
                    continue
            lines.append(current.rstrip())
            current = ch.lstrip() if ch == " " else ch
        else:
            current = candidate
    if current.strip():
        lines.append(current.strip())
    return lines


def _limit_lines(lines: list[str], max_lines: int) -> list[str]:
    """
    Keep at most `max_lines`, always marking elision.

    The marker is unconditional: when the kept line is a single character there
    is nothing to trim, but the text after it was still dropped, and silently
    losing the rest of a sentence reads as a rendering bug rather than as a
    deliberate "this line is too long to show in full".
    """
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    kept[-1] = kept[-1] + "…"
    return kept


# --------------------------------------------------------------------------
# Duration probing
# --------------------------------------------------------------------------

_duration_cache: dict[str, float] = {}


def probe_duration(path: str) -> Optional[float]:
    """
    Duration of an audio file in seconds, or None when it cannot be read.

    Cached: a video with a few hundred segments would otherwise shell out to
    ffprobe once per segment on every single export.
    """
    if not path:
        return None
    cached = _duration_cache.get(path)
    if cached is not None:
        return cached

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

    _duration_cache[path] = duration
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


def build_cues(
    segments: Iterable,
    style: SubtitleStyle,
    *,
    probe_audio: bool = True,
) -> list[Cue]:
    """
    Turn pipeline segments into display cues with adapted timing.

    Ordering is by start time (ASR output is normally ordered, but a recovered
    state file need not be). Each cue:

      * starts at the segment's original start,
      * lasts at least `min_duration`,
      * is extended to cover the ACTUAL dubbed audio when it is longer,
      * is clamped just before the next cue so two never overlap on screen.
    """
    ordered = sorted(
        (s for s in segments if (getattr(s, "translated_text", "") or "").strip()
         or (getattr(s, "text", "") or "").strip()),
        key=lambda s: getattr(s, "start_time", 0.0) or 0.0,
    )

    cues: list[Cue] = []
    for index, seg in enumerate(ordered):
        primary, secondary = _select_text(seg, style.track)
        if not primary:
            continue

        start = max(0.0, float(getattr(seg, "start_time", 0.0) or 0.0))
        end = max(float(getattr(seg, "end_time", start) or start), start + style.min_duration)

        # Cover the real dub. Speech-rate fitting can leave the audio longer
        # than the ASR slot, and a cue that ends early looks like a bug.
        if probe_audio:
            audio_path = getattr(seg, "audio_path", None)
            if audio_path and os.path.exists(audio_path):
                audio_duration = probe_duration(audio_path)
                if audio_duration:
                    end = max(end, start + audio_duration)

        # Never overlap the next cue.
        if index + 1 < len(ordered):
            next_start = float(getattr(ordered[index + 1], "start_time", 0.0) or 0.0)
            if next_start > start:
                end = min(end, max(start + 0.2, next_start - 0.04))

        cues.append(Cue(start=start, end=max(end, start + 0.2), text=primary, secondary=secondary))

    return cues


def _select_text(seg, track: str) -> tuple[str, str]:
    translated = (getattr(seg, "translated_text", "") or "").strip()
    original = (getattr(seg, "text", "") or "").strip()
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
    seconds = max(0.0, seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):d}:{int(minutes):02d}:{secs:05.2f}"


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


def to_ass(cues: list[Cue], style: SubtitleStyle, width: int, height: int) -> str:
    """
    ASS subtitle text, sized for THIS video.

    `PlayResX/PlayResY` are the real frame size and every size is derived from
    it, which is what makes one style work across resolutions. ASS is also the
    only way to carry real styling in a remux (`-c:v copy`) — MP4's `mov_text`
    supports no styling at all.
    """
    font_size = max(8, round(height * style.font_size_percent / 100.0))
    margin_v = max(0, round(height * style.margin_v_percent / 100.0))
    margin_h = max(0, round(width * style.margin_h_percent / 100.0))

    # BorderStyle 3 draws an opaque box behind the text; 1 draws only the
    # outline. "box" in the UI maps to 3 with a translucent BackColour.
    border_style = 3 if style.background == "box" else 1
    back_color = "&H80000000" if style.background == "box" else "&HFF000000"
    border_and_shadow = "yes"

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
        ",".join(
            [
                "Style: Default",
                style.font_family,
                str(font_size),
                _ass_color(style.primary_color),
                _ass_color(style.primary_color),
                _ass_color(style.outline_color),
                back_color,
                "-1" if style.bold else "0",
                "0", "0", "0",
                "100", "100", "0", "0",
                str(border_style),
                f"{style.outline_width:g}",
                f"{style.shadow:g}",
                str(_ASS_ALIGNMENT.get(style.alignment, 2)),
                str(margin_h), str(margin_h), str(margin_v),
                "1",
            ]
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue in cues:
        body = cue.lines(style)
        if not body:
            continue
        # \N is an explicit break, \n would be a soft one.
        text = "\\N".join(body)
        text = text.replace("{", "(").replace("}", ")")   # keep override tags out
        lines.append(
            f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Default,,"
            f"0,0,0,,{text}"
        )

    return "\n".join(lines) + "\n"


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
        "burn_server": {
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
