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
    # "client" (browser runs MDX-Net), "server" (PyTorch) or "off"
    separation_mode: str = "client"
    bgm_separation_available: bool = False
    enable_bgm_separation: bool = False
    enable_voice_clone: bool = False


# ----- Pipeline -----

class ProcessRequest(BaseModel):
    target_language: str = "English"
    # Default comes from config.ENABLE_BGM_SEPARATION_DEFAULT (False: the
    # separation model needs PyTorch and is not viable on a small CPU host).
    enable_bgm_separation: bool = False
    enable_voice_clone: bool = False
    # Mux the dubbed audio into a downloadable MP4 when finished.
    export_video: bool = True


# ----- Export -----

class ExportResponse(BaseModel):
    video_id: str
    url: str
    size_mb: Optional[float] = None
