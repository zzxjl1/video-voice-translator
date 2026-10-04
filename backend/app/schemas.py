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
    enable_bgm_separation: bool = False
    enable_voice_clone: bool = False
    # Mux the dubbed audio into a downloadable MP4 when finished.
    export_video: bool = True


# ----- Export -----

class ExportResponse(BaseModel):
    video_id: str
    url: str
    size_mb: Optional[float] = None
