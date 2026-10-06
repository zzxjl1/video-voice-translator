"""
Pydantic schemas for API request/response models.
"""
from pydantic import BaseModel
from typing import Optional


# ----- Upload -----

class UploadResponse(BaseModel):
    video_id: str
    filename: str
    exists: bool = False
    status: str = "uploaded"


# ----- Transcription -----

class SegmentOut(BaseModel):
    id: str
    speaker_id: str
    speaker_label: str
    start_time: float
    end_time: float
    text: str
    translated_text: str = ""
    audio_url: Optional[str] = None


class TranscribeResponse(BaseModel):
    video_id: str
    status: str
    segments: list[SegmentOut] = []
    error: Optional[str] = None


# ----- Translation -----

class TranslationSegmentIn(BaseModel):
    id: str
    text: str
    speaker_id: str
    start_time: float
    # Used to compute the per-segment length budget so the translation fits
    # the time slot it has to fill. Optional for backward compatibility.
    end_time: float = 0.0


class TranslateRequest(BaseModel):
    target_language: str = "English"
    segments: list[TranslationSegmentIn]


class TranslationResultItem(BaseModel):
    id: str
    translated_text: str


class TranslateResponse(BaseModel):
    video_id: str
    translations: list[TranslationResultItem] = []
    error: Optional[str] = None


# ----- TTS -----

class TTSRequest(BaseModel):
    segment_id: str
    text: str
    voice: Optional[str] = None
    # Length of the time slot this line has to fill. When given, the backend
    # measures the result and re-synthesizes once if it misses — this is what
    # the "Refit" action in the transcript panel uses.
    target_duration: Optional[float] = None
    # Used only to predict a good starting speed. Without it the first pass
    # uses the default rate, and the measurement/correction still fits the line.
    target_language: Optional[str] = None


class TTSResponse(BaseModel):
    audio_url: str
    content_type: str = "audio/mp3"


# ----- Status -----

class SpeakerOut(BaseModel):
    id: str
    name: str


class VideoStatusResponse(BaseModel):
    video_id: str
    filename: str
    status: str
    error: Optional[str] = None
    segments: list[SegmentOut] = []
    speakers: list[SpeakerOut] = []
    has_vocals: bool = False
    has_background: bool = False
    has_asr: bool = False
    has_translation: bool = False
    has_tts: bool = False
    has_export: bool = False
    export_url: Optional[str] = None
    export_available: bool = False
    # "mp4" or "mkv"; None when nothing has been exported yet.
    export_container: Optional[str] = None
    # Subtitle state for this video, so the export panel can initialise without
    # extra round-trips: the look, the track/container plan, and which formats
    # this server can actually produce (with reasons).
    subtitle_style: dict = {}
    subtitle_export: dict = {}
    subtitle_capabilities: dict = {}
    # Separation backend the pipeline should use for this video:
    # "client" (browser runs MDX-Net and uploads stems), "api" (302.AI) or "off".
    separation_mode: str = "client"
    # Per-backend availability + the reason an option is unusable:
    # {"client": {"available": bool, "reason": str|None}, "api": {...}, "off": {...}}
    separation_backends: dict = {}
    enable_bgm_separation: bool = False
    enable_voice_clone: bool = False


# ----- Pipeline -----

class ProcessRequest(BaseModel):
    target_language: str = "English"
    # Separation backend for this job. Validated server-side against
    # config.VALID_SEPARATION_MODES and config.separation_capabilities(); the
    # client's choice is a request, not an instruction.
    separation_mode: Optional[str] = None
    # Accepted but IGNORED, kept only so that a stale client or a hand-made
    # request still validates instead of getting a 422. Whether the browser
    # already produced the stems is answered by the files on disk, not by a flag
    # from the client, and `separation_mode` is the single source of truth.
    enable_bgm_separation: bool = False
    enable_voice_clone: bool = False
    # Mux the dubbed audio into a downloadable MP4 when finished.
    export_video: bool = True


# ----- Export -----

class SubtitleExportRequest(BaseModel):
    """
    Which subtitle tracks to embed, and in what container.

    Mirrors `subtitle_service.ExportPlan`. Every field is optional on purpose:
    the request is merged over the video's saved plan, so a client that only
    flips one switch does not silently reset the rest.
    """

    enabled: Optional[bool] = None
    # "off" | "soft" (MP4) | "styled" (MKV + ASS) | "burn"
    format: Optional[str] = None
    tracks: Optional[list[str]] = None
    default_track: Optional[str] = None
    style: Optional[dict] = None


class ExportRequest(BaseModel):
    """Body of POST /videos/{id}/export. The whole body is optional."""

    subtitles: Optional[SubtitleExportRequest] = None


class ExportResponse(BaseModel):
    video_id: str
    url: str
    size_mb: Optional[float] = None
    # "mp4" or "mkv" — the filename depends on it and the client shows it.
    container: str = "mp4"
    filename: Optional[str] = None
    # One entry per embedded track: {track, title, language, codec, default}
    subtitle_tracks: list[dict] = []
