"""
项目文档的数据结构与路径。

一个工程 = 一份 `project.json`（**唯一**的状态来源）+ 只追加的 `events.jsonl` /
`chat.jsonl` + 媒体文件（原视频、音轨、分离产物、tts/*.mp3、导出）。

读写与变更全部在 `app/store.py`：这里只放"长什么样"和"文件在哪"。

和旧版的根本区别：
  · 一行分段不再由五个文件在读取时拼出来（asr_result / translation_result /
    tts_results / segment_flags / enhanced_transcript），也不再靠"目录里有没有
    mp3"去猜配音做没做过 —— 全部在这份文档里，译文和配音各自带一个**指纹**，
    指纹对不上就是过期（见 `app/status.py`）。
  · 没有"进度状态机"（EXTRACTING_AUDIO / TRANSLATED / …）和 `_detect_resume_phase`：
    "做到哪了"就是"哪些行缺译文、哪些行缺配音"，每次都从文档算出来。
"""
import datetime
import json
import logging
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Optional

from app import config

logger = logging.getLogger(__name__)

PROJECT_FILE = "project.json"
EVENTS_FILE = "events.jsonl"
CHAT_FILE = "chat.jsonl"

SEGMENT_KINDS = ("speech", "music", "noise")

# 工程阶段：只有"初始化过没有"这一件事需要状态机；其余都是从行数据算出来的。
STAGE_UPLOADED = "uploaded"          # 已上传，还没开始初始化
STAGE_INITIALIZING = "initializing"  # 初始化任务在跑
STAGE_READY = "ready"                # 初始化完成，可以编辑 / 聊天
STAGE_FAILED = "failed"              # 初始化失败（ASR 都没出来），可重试


@dataclass
class Speaker:
    id: str                              # ASR 的 speaker_id（"0"、"1"…），音色按它记
    name: str                            # 展示名，用户/AI 可改
    voice: Optional[str] = None          # 系统音色（自动分配或手动指定，都在这）
    # 克隆音色：{"voice_id", "target_model", "created_at"}。绑定 TTS 模型，换模型即失效。
    clone: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data) -> "Speaker":
        if isinstance(data, cls):
            return data
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Segment:
    id: str
    speaker_id: str
    start_time: float
    end_time: float
    text: str                            # 原文（ASR，或用户/AI 改过的）
    translated_text: str = ""
    # speech 正常；music / noise 是 ASR 把非语音误识成了话 —— 不翻译、不配音、不出字幕。
    kind: str = "speech"
    # 静音：不合成配音，这一格没有声音。隐藏：这一行不出字幕。两者互补。
    muted: bool = False
    hidden: bool = False
    # omni 审听版（纠错 + 语气标签）。有它就用它做翻译基底；原文被改动时清空。
    reviewed_text: str = ""
    # 译文是用哪份（原文, 目标语言, 自定义要求）翻出来的。对不上 = 译文过期。
    tr_fp: str = ""
    # 本行配音的单独设置（空 = 跟随说话人/工程）。
    tts_voice: Optional[str] = None
    tts_rate: Optional[float] = None
    tts_instruction: Optional[str] = None
    # 当前这份 mp3 是用哪组（文本, 音色, 语速, 指令, 模型, 语言）合成的；
    # 与"现在应有的指纹"对不上 = 配音过期。
    tts_fp: str = ""
    # 配音实际时长（秒）：混音计划用它算 atempo。合成时量得，随指纹一起落盘。
    actual_duration: Optional[float] = None
    # —— 以下是读出时算出来的派生值，**不落盘** ——
    audio_path: Optional[str] = None     # 仅当配音"新鲜"时才有（见 status.hydrate）

    _DERIVED = ("audio_path",)

    def to_dict(self) -> dict:
        data = asdict(self)
        for key in self._DERIVED:
            data.pop(key, None)
        return data

    @classmethod
    def from_dict(cls, data) -> "Segment":
        if isinstance(data, cls):
            return data
        known = {f.name for f in fields(cls)} - set(cls._DERIVED)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class VideoState:
    video_id: str                        # 文件 MD5
    filename: str
    file_path: str
    stage: str = STAGE_UPLOADED
    init_error: Optional[str] = None
    # 最近一次初始化的报告（配置快照 + 每步结果），聊天里的第一条消息据此生成。
    init_report: dict = field(default_factory=dict)
    # 当前聊天会话的 id。空 = 还没开始（第一条消息时自动开一个）。历史会话都在
    # 同一个 chat.jsonl 里，每行带 conv 字段 —— 追加式写入，切换会话只是换个过滤条件。
    chat_conv: str = ""

    # —— 工程设置 ——
    separation_mode: str = ""            # "client"（浏览器分离）| "off"；初始化时定下
    enable_voice_clone: bool = False
    enable_dubbing: bool = True          # False = 仅字幕：不分离/不克隆/不合成，导出原声直通
    target_language: str = ""
    accent: Optional[str] = None         # 中文方言（"广东话"…）；None = 普通话
    custom_prompt: str = ""              # 注入翻译的自定义要求
    mm_enhance: bool = False             # omni 审听增强（纠错 + 语气标签）
    clone_smart_pick: bool = False       # 克隆素材由 omni 挑选

    segments: list = field(default_factory=list)   # list[Segment]
    speakers: list = field(default_factory=list)   # list[Speaker]

    # 字幕外观 / 导出方案：普通 dict，空 = 用配置默认（见 subtitle_service）。
    subtitle_style: dict = field(default_factory=dict)
    subtitle_export: dict = field(default_factory=dict)

    rev: int = 0                         # 每次状态变更 +1；客户端据此判断"我落后了没有"
    created_at: str = field(default_factory=lambda: datetime.datetime.now().isoformat())

    def to_dict(self) -> dict:
        data = asdict(self)
        data["segments"] = [s.to_dict() for s in self.segments]
        data["speakers"] = [s.to_dict() for s in self.speakers]
        return data

    @classmethod
    def from_dict(cls, data) -> "VideoState":
        if isinstance(data, cls):
            return data
        data = dict(data)
        segments = [Segment.from_dict(s) for s in data.pop("segments", [])]
        speakers = [Speaker.from_dict(s) for s in data.pop("speakers", [])]
        # 状态文件里出现本版本不认识的键，不该让工程读不出来。
        known = {f.name for f in fields(cls)}
        state = cls(**{k: v for k, v in data.items() if k in known})
        state.segments = segments
        state.speakers = speakers
        return state

    def segment(self, segment_id: str) -> Optional[Segment]:
        return next((s for s in self.segments if s.id == segment_id), None)

    def speaker(self, speaker_id: str) -> Optional[Speaker]:
        return next((s for s in self.speakers if s.id == speaker_id), None)


# ----------------------------------------------------------------------------
# 路径与文件
# ----------------------------------------------------------------------------

def video_dir_path(video_id: str) -> str:
    """工程目录路径，**不创建任何东西**（只读路径用它，免得一个 GET 就能建目录）。"""
    return os.path.join(config.DATA_DIR, video_id)


def get_video_dir(video_id: str) -> str:
    """工程目录路径，并确保存在（写方用）。"""
    path = video_dir_path(video_id)
    os.makedirs(path, exist_ok=True)
    return path


def tts_audio_path(video_id: str, segment_id: str) -> str:
    return os.path.join(video_dir_path(video_id), "tts", f"{segment_id}.mp3")


def asr_audio_path(video_id: str) -> Optional[str]:
    """
    给 ASR / omni 听的 16 kHz 单声道音轨：有人声分离结果用它，否则用整条抽出的音频。
    都还没生成时返回 None。
    """
    base = video_dir_path(video_id)
    for name in ("vocals_asr.wav", "extracted_audio.wav"):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    return None


def atomic_write_json(path: str, data) -> None:
    """
    写 JSON：先写同目录的 `.part`，再 `os.replace` 覆盖目标 —— 读者看到的要么是旧文件、
    要么是新文件，不会是写了一半的 JSON。进程在写的中途被杀只会留下一个 `.part`
    残留（下次写入会覆盖它，没有任何代码读它）。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.part"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default
