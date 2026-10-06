"""
Subtitle endpoints.

Two routers, because the resources genuinely live at different levels:

  * `router`       (/subtitles)  — server-wide: what this host can produce, and
                                    the default style.
  * `video_router` (/videos)     — per video: the SRT download, the cue list the
                                    preview overlay renders, and the saved style.

Why the cues come from the server
---------------------------------
There is a `subtitles/cues` endpoint even though the frontend already holds the
segments. The cues are NOT the segments: `build_cues` extends each one to cover
the dubbed audio that actually exists and clamps it against its neighbour, which
requires probing audio durations. Reimplementing that in the browser would drift
from what the export produces, and the whole point of the preview is that it
matches. Timing therefore comes from here; only the cosmetic wrapping is allowed
to be recomputed client-side while a slider is being dragged.

On burning
----------
There is deliberately no burn endpoint. Rendering text into frames needs libass
plus an installed CJK font and forces a full video re-encode, which this class of
host cannot do — `capabilities()["burn_server"]` reports exactly that, and the
UI greys the option out. Burning happens in the browser instead (Canvas2D +
MediaRecorder), where it costs the server nothing.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app import config
from app.models import get_state, save_state
from app.services import subtitle_service
from app.services.subtitle_service import SubtitleStyle

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/subtitles", tags=["subtitles"])
video_router = APIRouter(prefix="/videos", tags=["subtitles"])

# Text for the style editor's preview cue, when the playhead is not on a line.
#
# Generated HERE, and not in the browser, for the same reason the cues are:
# line breaking has to be `_wrap`, or the sample would break lines differently
# from the export and quietly teach the user the wrong thing about
# `max_chars_per_line`. `Cue.lines()` already does that work for any style.
#
# Per track rather than one string for all three, because the tracks need
# opposite things. A single-track sample is most useful long enough to wrap;
# a bilingual sample has to fit BOTH languages inside `max_lines` (2 by
# default), so each half must stay on one line. One string cannot do both.
_SAMPLE_TEXT = {
    "translated": ("这是一行示例字幕，用来预览字号、颜色和位置。", ""),
    "original": ("This is a sample subtitle for previewing the style.", ""),
    "bilingual": ("这是一行示例字幕。", "A sample subtitle line."),
}


def _sample_cue(style: SubtitleStyle) -> dict:
    """
    A cue that is not in the video, for previewing a style on an empty frame.

    Deliberately NOT a real cue from the video: opening the editor on a gap
    between lines would otherwise show nothing, and the user would be dragging
    sliders against a bare picture. A fixed string also means the preview is
    stable while sliders move, instead of jumping between lines.

    Wrapped by `Cue.lines()`, so it obeys `max_chars_per_line`, `max_lines` and
    (for bilingual) the two-line pairing exactly like a real cue.
    """
    primary, secondary = _SAMPLE_TEXT.get(style.track, _SAMPLE_TEXT["translated"])
    cue = subtitle_service.Cue(start=0.0, end=0.0, text=primary, secondary=secondary)
    return {
        "start": 0.0,
        "end": 0.0,
        "text": cue.text,
        "secondary": cue.secondary,
        "lines": cue.lines(style),
    }


def _style_for(state) -> SubtitleStyle:
    """
    The style to use for a video: its saved one, or the configured default.

    Falls back rather than failing when the stored blob is unusable, so a bad
    value can never make a video permanently un-exportable.
    """
    saved = getattr(state, "subtitle_style", None)
    if not saved:
        return subtitle_service.default_style()
    try:
        return SubtitleStyle.from_dict(saved)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[%s] Stored subtitle style is unusable (%s); using defaults",
            getattr(state, "video_id", "?"),
            e,
        )
        return subtitle_service.default_style()


def _safe_filename_track(track: str) -> str:
    """Only the three known tracks may reach a filename or a header."""
    return track if track in SubtitleStyle.VALID_TRACKS else "translated"


# ---------------------------------------------------------------------------
# Server-wide
# ---------------------------------------------------------------------------


@router.get("/capabilities")
async def get_capabilities():
    """
    Which subtitle deliveries this server can actually perform, and why not.

    Lets the UI grey out an option with a concrete reason instead of offering
    something that fails halfway through an export — the same shape as
    `GET /api/models/separator` uses for the separation backends.
    """
    return {
        "capabilities": subtitle_service.capabilities(),
        "default_style": subtitle_service.default_style().to_dict(),
    }


@router.get("/defaults")
async def get_defaults():
    """The style a video starts with (config-derived)."""
    return {"style": subtitle_service.default_style().to_dict()}


# ---------------------------------------------------------------------------
# Per video
# ---------------------------------------------------------------------------


@video_router.get("/{video_id}/subtitles/style")
async def get_style(video_id: str):
    """The style in effect for this video, plus the server default for reset."""
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")
    return {
        "video_id": video_id,
        "style": _style_for(state).to_dict(),
        "default_style": subtitle_service.default_style().to_dict(),
        "is_custom": bool(getattr(state, "subtitle_style", None)),
    }


@video_router.put("/{video_id}/subtitles/style")
async def put_style(video_id: str, style: dict):
    """
    Save this video's subtitle style.

    The body is taken as a plain dict and normalised by `SubtitleStyle`, so a
    stale client sending an unknown key gets it ignored rather than a 422, and
    out-of-range numbers are clamped instead of corrupting every later export.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    normalised = SubtitleStyle.from_dict(style)
    state.subtitle_style = normalised.to_dict()
    save_state(state)
    return {"video_id": video_id, "style": state.subtitle_style}


@video_router.get("/{video_id}/subtitles/cues")
async def get_cues(video_id: str, track: str = Query(default="")):
    """
    Display cues with adapted timing, for the preview overlay.

    `track` overrides the saved style's track so the UI can preview "original"
    or "bilingual" without persisting the change first.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    style = _style_for(state)
    if track:
        style = SubtitleStyle.from_dict({**style.to_dict(), "track": track})

    cues = subtitle_service.build_cues(state.segments, style)
    return {
        "video_id": video_id,
        "track": style.track,
        "count": len(cues),
        "style": style.to_dict(),
        "sample": _sample_cue(style),
        "cues": [
            {
                "start": round(cue.start, 3),
                "end": round(cue.end, 3),
                "text": cue.text,
                "secondary": cue.secondary,
                # Advisory: the client may re-wrap while a slider moves, but
                # these are what the export will produce.
                "lines": cue.lines(style),
            }
            for cue in cues
        ],
    }


@video_router.post("/{video_id}/subtitles/preview")
async def preview_cues(video_id: str, body: dict):
    """
    Cues rendered with a style the caller supplies, WITHOUT saving it.

    Exists so the style editor can show a live result on the player while the
    user drags a slider. The alternative — re-wrapping in JavaScript — would
    mean a second implementation of the line-breaking rules, and the preview
    would silently drift from the export the moment the two disagreed. Timing
    has the same problem: it depends on probed audio durations.

    Body: `{"track": "translated"?, "style": {...}?}` — both optional, merged
    over the video's saved style.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    base = _style_for(state).to_dict()
    patch = body.get("style") if isinstance(body, dict) else None
    if isinstance(patch, dict):
        # Only the keys the editor actually changed are sent.
        base = {**base, **patch}
    track = body.get("track") if isinstance(body, dict) else None
    if track:
        base = {**base, "track": track}

    style = SubtitleStyle.from_dict(base)
    cues = subtitle_service.build_cues(state.segments, style)
    return {
        "video_id": video_id,
        "track": style.track,
        "count": len(cues),
        "style": style.to_dict(),
        # Not part of the video. The editor shows it when the playhead is not on
        # a line, so opening the panel never lands on an empty frame.
        "sample": _sample_cue(style),
        "cues": [
            {
                "start": round(cue.start, 3),
                "end": round(cue.end, 3),
                "text": cue.text,
                "secondary": cue.secondary,
                "lines": cue.lines(style),
            }
            for cue in cues
        ],
    }


@video_router.get("/{video_id}/subtitles.srt")
async def download_srt(video_id: str, track: str = Query(default="")):
    """
    Download the subtitles as a standalone .srt file.

    Costs nothing to produce (pure text) and is the format platforms and editors
    want, so it is offered on its own rather than only as an export side effect.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    style = _style_for(state)
    if track:
        style = SubtitleStyle.from_dict({**style.to_dict(), "track": track})
    style = SubtitleStyle.from_dict({**style.to_dict(), "track": _safe_filename_track(style.track)})

    cues = subtitle_service.build_cues(state.segments, style)
    if not cues:
        raise HTTPException(
            status_code=409,
            detail="No subtitles yet — run transcription and translation first.",
        )

    body = subtitle_service.to_srt(cues, style)
    filename = f"{_safe_filename_track(style.track)}_{video_id[:8]}.srt"
    return Response(
        content=body,
        media_type="application/x-subrip; charset=utf-8",
        headers={
            # ASCII filename only: a non-ASCII name in Content-Disposition needs
            # RFC 5987 encoding that not every client honours, and the track
            # name is already ASCII.
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
