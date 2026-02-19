"""
In-memory state models for per-video processing.
"""
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


class VideoStatus(str, Enum):
    UPLOADED = "uploaded"
    EXTRACTING_AUDIO = "extracting_audio"
    TRANSCRIBING = "transcribing"
    TRANSCRIBED = "transcribed"
    TRANSLATING = "translating"
    TRANSLATED = "translated"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class Segment:
    id: str
    speaker_id: str
    speaker_label: str
    start_time: float
    end_time: float
    text: str
    translated_text: str = ""
    audio_path: Optional[str] = None


@dataclass
class VideoState:
    video_id: str  # MD5 hash
    filename: str
    file_path: str
    audio_path: Optional[str] = None
    status: VideoStatus = VideoStatus.UPLOADED
    error_message: Optional[str] = None
    segments: list[Segment] = field(default_factory=list)


# Global in-memory state store keyed by video_id (MD5)
video_states: dict[str, VideoState] = {}


def get_or_create_state(video_id: str, filename: str = "", file_path: str = "") -> VideoState:
    if video_id not in video_states:
        video_states[video_id] = VideoState(
            video_id=video_id,
            filename=filename,
            file_path=file_path,
        )
    return video_states[video_id]


def get_state(video_id: str) -> Optional[VideoState]:
    return video_states.get(video_id)
