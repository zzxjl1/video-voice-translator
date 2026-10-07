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
    muted: bool = False


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


class MuteRequest(BaseModel):
    """静音 / 取消静音某一行。"""
    muted: bool = True


class TranslateRequest(BaseModel):
    target_language: str = "English"
    segments: list[TranslationSegmentIn]
    # Optional requester instructions injected into the translation prompts.
    # Clamped server-side; empty means default behaviour.
    custom_prompt: str = ""
    # 单行重译的长度意图："longer" / "shorter" / None。影响该行的长度预算与
    # 提示词，用于「长一点 / 短一点」。非法值当作 None（宽容）。
    length_hint: Optional[str] = None


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
    # Chinese dialect to speak in, e.g. "广东话". Empty/absent means Mandarin.
    # Validated against config.CHINESE_ACCENTS and ignored for other languages;
    # see config.tts_instruction for why this is checked and not trusted.
    accent: Optional[str] = None


class TTSResponse(BaseModel):
    audio_url: str
    content_type: str = "audio/mp3"


class SpeakerVoiceRequest(BaseModel):
    """Pin one speaker to one system voice (voice cloning off)."""

    speaker_id: str
    voice: str


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
    # Live workflow state (active=false when nothing is running):
    # {active, step, step_status, progress, total, cancelling, elapsed_s, started_at}
    run: dict = {}
    # 上一次被用户取消时停在哪个阶段（None = 没取消过）。持久化的，所以重开
    # 页面也知道"上次取消在哪一步、能不能接着跑"。
    cancelled_step: Optional[str] = None
    enable_bgm_separation: bool = False
    enable_voice_clone: bool = False
    # The dub's target language and Chinese accent as stored on THIS project, so
    # a recovered session resumes the project's own choices instead of whatever
    # the current browser would guess. Empty/None means the pipeline has not
    # chosen yet and the client keeps its initial values.
    target_language: str = ""
    accent: Optional[str] = None
    # Requester instructions injected into translation; persisted on the
    # project so reprocess and recovery replay the same behaviour.
    custom_prompt: str = ""
    # 这两个开关随工程持久化，恢复会话时前端要原样拿回。
    mm_enhance: bool = False
    clone_smart_pick: bool = False


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
    # 语气模仿：TTS 前由 omni 听原声给译文注入情感/拟声标签（见 config）。
    mm_enhance: bool = False
    # 智能选材：克隆参考素材由 omni 通过 tool call 挑选，失败回退规则。
    # 仅当 enable_voice_clone 为真时有意义。
    clone_smart_pick: bool = False
    # Chinese dialect for the dubbed audio, e.g. "广东话". Absent means Mandarin.
    # Only meaningful with target_language="Chinese"; ignored otherwise.
    accent: Optional[str] = None
    # Optional requester instructions injected into the DS translation prompts
    # ("保持专有名词原文"…). Clamped server-side; empty means default behaviour.
    custom_prompt: str = ""
    # NOTE: there is deliberately no export flag here. Muxing the dub into a
    # video is user-triggered (POST /videos/{id}/export), never a pipeline
    # stage; a request field for it only invited silent auto-export.


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
