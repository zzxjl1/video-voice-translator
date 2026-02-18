"""
Pydantic schemas for API request/response models.
"""
from pydantic import BaseModel
from typing import Optional


# ----- Upload -----

class UploadResponse(BaseModel):
    video_id: str
    filename: str


# ----- Transcription -----

class SegmentOut(BaseModel):
    id: str
    speaker_label: str
    start_time: float
    end_time: float
    text: str
    translated_text: str = ""


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
    text: str
    voice: Optional[str] = None


class TTSResponse(BaseModel):
    audio_base64: str
    content_type: str = "audio/mp3"


# ----- Status -----

class VideoStatusResponse(BaseModel):
    video_id: str
    filename: str
    status: str
    error: Optional[str] = None
    segments: list[SegmentOut] = []
