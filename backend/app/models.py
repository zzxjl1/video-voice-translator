import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
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
    # 用户主动取消（不是失败，也不是"暂停"—— 这活儿不会再自己继续）。
    # 语义是"可续跑"：磁盘上的阶段产物都还在，下次运行从最后一个完成的阶段
    # 接着走。和 ERROR 分开，是为了让界面能区分"它坏了"和"我取消的"，也让
    # 前端不要自动重跑一个用户刚取消的工程。
    CANCELLED = "cancelled"


@dataclass
class Speaker:
    id: str
    name: str

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, cls):
            return data
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
    # 该行静音：不合成配音，导出时这一段保持无声（原声轨在整条时间轴上已
    # 被配音轨取代，所以"静音"就是这一格没有任何配音音频）。
    muted: bool = False
    # 隐藏 = 这一行不出字幕：预览叠加、SRT 下载、导出内嵌字幕一律跳过它，
    # 但它的配音照常在时间轴上。和 muted 互补 —— muted 管声音，hidden 管画面
    # 上的字。
    hidden: bool = False

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, cls):
            return data
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
    # Which backend performed separation: "client" (browser)
    # or "off". Persisted so a resumed run keeps the same choice. Empty string
    # means "no pipeline has run yet", so the status endpoint reports the server
    # default for a project that has not started instead of reading as one that
    # was deliberately run with separation off.
    separation_mode: str = ""
    enable_voice_clone: bool = False
    # Subtitle look, as a plain dict so it round-trips through JSON untouched.
    # Empty means "use config defaults" — see subtitle_service.SubtitleStyle.
    # Persisted per video so a resumed run (and the pipeline's own export step)
    # renders the same subtitles the user configured.
    subtitle_style: dict = field(default_factory=dict)
    # Which subtitle tracks the export embeds and in which container. Same
    # contract as `subtitle_style`: a plain dict, empty means "use config
    # defaults". See subtitle_service.ExportPlan.
    subtitle_export: dict = field(default_factory=dict)
    # Target language of the dub. Stored because the exported subtitle tracks
    # need an ISO language tag so players can auto-select one, and the tag
    # cannot be recovered from the segments after the fact.
    target_language: str = ""
    # Chinese dialect requested for the dub ("广东话"…). None means Mandarin.
    # Persisted because the client otherwise sends the accent per request: after
    # a session recovery the client only knows its own default, and a refit or
    # single-line regen would quietly switch a Cantonese dub back to Mandarin.
    accent: Optional[str] = None
    # 用户取消时停在哪个阶段（separation/asr/mm_enhance/translation/
     # voice_clone/tts）。持久化是为了重开页面还能说清"上次取消在哪一步、能不能
     # 跑" —— 内存里的运行状态随进程消失，这个不会。跑起来时清空。
    cancelled_step: Optional[str] = None
    # Free-form instructions the requester injected into the translation step.
    # Persisted so a reprocess (which re-translates from scratch) and a session
    # recovery both replay the SAME translation behaviour the dub was made
    # with — an injected requirement is a property of the project, not of the
    # tab it was typed in. Empty string = default translation behaviour.
    custom_prompt: str = ""
    # 语气模仿：TTS 前由 omni 听原声给译文注入情感/拟声标签。随工程持久化，
    # 理由同 accent —— 恢复会话后重配音的行必须保持同样的处理方式。
    mm_enhance: bool = False
    # 智能选材：克隆参考素材由 omni 通过 tool call 挑选（失败回退规则）。
    # 只在 enable_voice_clone 为真时有意义。
    clone_smart_pick: bool = False
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

    def to_dict(self):
        data = asdict(self)
        data["status"] = self.status.value
        data["segments"] = [s.to_dict() for s in self.segments]
        data["speakers"] = [s.to_dict() for s in self.speakers]
        return data

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, cls):
            return data
            
        segments_data = data.pop("segments", [])
        speakers_data = data.pop("speakers", [])
        status_val = data.pop("status", VideoStatus.UPLOADED.value)
        # 只保留本版本认识的字段：`cls(**data)` 遇到未知键会抛 TypeError，而那
        # 意味着"删掉一个字段" = "所有写过那个字段的老工程都读不出来"。这不是
        # 兼容补丁，是"状态文件不认识某个字段也不该让工程报废"。
        known_fields = {f.name for f in fields(cls)}
        data = {k: v for k, v in data.items() if k in known_fields}
        # Handle potential invalid status values
        try:
            status = VideoStatus(status_val)
        except ValueError:
            status = VideoStatus.UPLOADED
            
        state = cls(status=status, **data)
        state.segments = [Segment.from_dict(s) for s in segments_data]
        state.speakers = [Speaker.from_dict(s) for s in speakers_data]
        return state


# State persistence functions

def get_video_dir(video_id: str) -> str:
    """Get the directory path for a specific video."""
    video_dir = os.path.join(config.DATA_DIR, video_id)
    os.makedirs(video_dir, exist_ok=True)
    return video_dir


def save_state(state: VideoState):
    """Save video state to the filesystem."""
    video_id = state.video_id
    video_dir = get_video_dir(video_id)
    state_path = os.path.join(video_dir, "info.json")
    
    # Save root info (metadata only, no segments to avoid redundancy)
    data = state.to_dict()
    data.pop("segments", None)
    
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved metadata to {state_path}")


SEGMENT_FLAGS_FILENAME = "segment_flags.json"


def set_segment_flag(video_id: str, segment_id: str, flag: str, value: bool) -> None:
    """
    Persist ONE boolean flag for one segment ("muted" | "hidden").

    The whole dict is read and written back on purpose: the file holds a list
    per flag, and writing only the key being changed would silently drop the
    other one (mute a line, hide another, and the first list disappears).
    """
    video_dir = get_video_dir(video_id)
    path = os.path.join(video_dir, SEGMENT_FLAGS_FILENAME)

    data: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, json.JSONDecodeError):
        data = {}

    ids = [i for i in (data.get(flag) or []) if i != segment_id]
    if value:
        ids.append(segment_id)
    data[flag] = ids

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)


def load_state(video_id: str) -> Optional[VideoState]:
    """
    Load video state by merging multiple files:
    1. info.json (metadata)
    2. asr_result.json (transcription)
    3. translation_result.json (translation)
    4. tts_results.json (synthesis)
    """
    video_dir = os.path.join(config.DATA_DIR, video_id)
    info_path = os.path.join(video_dir, "info.json")
    asr_path = os.path.join(video_dir, "asr_result.json")
    trans_path = os.path.join(video_dir, "translation_result.json")
    tts_results_path = os.path.join(video_dir, "tts_results.json")
    
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
                trans_map = {item["id"]: item["translatedText"] for item in trans_data}
                for s in segments:
                    if s.id in trans_map:
                        s.translated_text = trans_map[s.id]

        # 4. Load Synthesis result mapping
        tts_map = {}
        if os.path.exists(tts_results_path):
            try:
                with open(tts_results_path, "r", encoding="utf-8") as f:
                    tts_map = json.load(f)
            except Exception as e:
                # 以前这里是 `pass`：文件坏掉时静默当成"一行都没合成过"，于
                # 是整个工程的配音在界面上凭空消失（而且没有任何线索）。读不
                # 出来是异常情况，必须留痕。
                logger.warning(
                    f"[{video_id}] tts_results.json is unreadable ({e}); "
                    f"falling back to scanning the tts/ directory for audio files"
                )
                
        if segments:
            for s in segments:
                if s.id in tts_map:
                    s.audio_path = os.path.join(video_dir, tts_map[s.id])
                else:
                    # Fallback to direct scan
                    audio_file = f"{s.id}.mp3"
                    disk_path = os.path.join(video_dir, "tts", audio_file)
                    if os.path.exists(disk_path):
                        s.audio_path = disk_path

        # 4.5 Segment flags (muted / hidden) — its own small file, because
        # `save_state` deliberately writes no segments at all.
        flags_path = os.path.join(video_dir, SEGMENT_FLAGS_FILENAME)
        if segments and os.path.exists(flags_path):
            try:
                with open(flags_path, "r", encoding="utf-8") as f:
                    flags = json.load(f)
                    muted_ids = set(flags.get("muted") or [])
                    hidden_ids = set(flags.get("hidden") or [])
                for s in segments:
                    s.muted = s.id in muted_ids
                    s.hidden = s.id in hidden_ids
            except Exception as e:
                # 同上：坏掉时静默丢弃，用户会看到"我明明静音/隐藏了，怎么又
                # 有声音/又有字幕了"，且无从查起。
                logger.warning(
                    f"[{video_id}] {SEGMENT_FLAGS_FILENAME} is unreadable ({e}); "
                    f"muted/hidden flags for this project are treated as unset"
                )

        # Assemble
        data["segments"] = segments
        return VideoState.from_dict(data)
        
    except Exception as e:
        logger.error(f"Failed to load state for {video_id}: {e}")
        return None


def get_or_create_state(video_id: str, filename: str = "", file_path: str = "") -> VideoState:
    # Always check disk first
    disk_state = load_state(video_id)
    if disk_state:
        return disk_state
    
    # Create new if not found
    state = VideoState(
        video_id=video_id,
        filename=filename,
        file_path=file_path,
    )
    save_state(state)  # Initial save
    return state


def get_state(video_id: str) -> Optional[VideoState]:
    """Get state directly from disk."""
    return load_state(video_id)
