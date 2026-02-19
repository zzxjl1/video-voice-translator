import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

from app import config

logger = logging.getLogger(__name__)

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
class Speaker:
    id: str
    name: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


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

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(**data)


@dataclass
class VideoState:
    video_id: str  # MD5 hash
    filename: str
    file_path: str
    audio_path: Optional[str] = None
    status: VideoStatus = VideoStatus.UPLOADED
    error_message: Optional[str] = None
    segments: list[Segment] = field(default_factory=list)
    speakers: list[Speaker] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

    def to_dict(self):
        data = asdict(self)
        data["status"] = self.status.value
        data["segments"] = [s.to_dict() for s in self.segments]
        data["speakers"] = [s.to_dict() for s in self.speakers]
        return data

    @classmethod
    def from_dict(cls, data):
        segments_data = data.pop("segments", [])
        speakers_data = data.pop("speakers", [])
        status_val = data.pop("status", VideoStatus.UPLOADED.value)
        # Handle potential invalid status values
        try:
            status = VideoStatus(status_val)
        except ValueError:
            status = VideoStatus.UPLOADED
            
        state = cls(status=status, **data)
        state.segments = [Segment.from_dict(s) for s in segments_data]
        state.speakers = [Speaker.from_dict(s) for s in speakers_data]
        return state


# Global in-memory state store keyed by video_id (MD5)
# This acts as a cache. The source of truth is the disk.
video_states: dict[str, VideoState] = {}


def get_video_dir(video_id: str) -> str:
    """Get the directory path for a specific video."""
    video_dir = os.path.join(config.DATA_DIR, video_id)
    os.makedirs(video_dir, exist_ok=True)
    return video_dir


def save_state(video_id: str):
    """Save video state to the filesystem. Root info.json only contains metadata."""
    state = video_states.get(video_id)
    if not state:
        return
    
    video_dir = get_video_dir(video_id)
    state_path = os.path.join(video_dir, "info.json")
    
    # Save root info (metadata only, no segments to avoid redundancy)
    data = state.to_dict()
    data.pop("segments", None)
    
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved metadata to {state_path}")


def load_state(video_id: str) -> Optional[VideoState]:
    """
    Load video state by merging multiple files:
    1. info.json (metadata)
    2. asr_result.json (transcription)
    3. translation_result.json (translation)
    """
    video_dir = os.path.join(config.DATA_DIR, video_id)
    info_path = os.path.join(video_dir, "info.json")
    asr_path = os.path.join(video_dir, "asr_result.json")
    trans_path = os.path.join(video_dir, "translation_result.json")
    
    if not os.path.exists(info_path):
            return None
    
    try:
        # 1. Load basic info
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            
        # 2. Load ASR segments if they exist
        segments = []
        if os.path.exists(asr_path):
            with open(asr_path, "r", encoding="utf-8") as f:
                asr_data = json.load(f)
                segments = [Segment.from_dict(s) for s in asr_data]
                
        # 3. Load Translations and merge
        if segments and os.path.exists(trans_path):
            with open(trans_path, "r", encoding="utf-8") as f:
                trans_data = json.load(f)
                # trans_data is expected to be list of {id, translatedText}
                trans_map = {item["id"]: item["translatedText"] for item in trans_data}
                for s in segments:
                    if s.id in trans_map:
                        s.translated_text = trans_map[s.id]

        # 4. Check for synthesized audio (optional, but good for completeness)
        tts_dir = os.path.join(video_dir, "tts")
        if segments and os.path.exists(tts_dir):
            for s in segments:
                audio_file = f"{s.id}.mp3"
                if os.path.exists(os.path.join(tts_dir, audio_file)):
                    s.audio_path = os.path.join(tts_dir, audio_file)

        # Assemble
        data["segments"] = segments
        return VideoState.from_dict(data)
        
    except Exception as e:
        logger.error(f"Failed to load state for {video_id}: {e}")
        return None


def get_or_create_state(video_id: str, filename: str = "", file_path: str = "") -> VideoState:
    # Check cache first
    if video_id in video_states:
        return video_states[video_id]
    
    # Check disk
    disk_state = load_state(video_id)
    if disk_state:
        video_states[video_id] = disk_state
        return disk_state
    
    # Create new
    state = VideoState(
        video_id=video_id,
        filename=filename,
        file_path=file_path,
    )
    video_states[video_id] = state
    save_state(video_id)  # Initial save
    return state


def get_state(video_id: str) -> Optional[VideoState]:
    """Get state from cache or disk."""
    if video_id in video_states:
        return video_states[video_id]
    
    disk_state = load_state(video_id)
    if disk_state:
        video_states[video_id] = disk_state
        return disk_state
    
    return None
