import datetime
import json
import logging
import os
import threading
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
    # 这段配音的实际时长（秒），合成时由 TTS 拟合步骤测得并写回。
    # 混音计划（mix_service）用它算每段的 atempo —— 在此之前它只存在于
    # 前端的临时状态里（且只在用户展开过那一段的播放器时才有值），服务端
    # 拿不到，导出与预览各猜各的。
    actual_duration: Optional[float] = None
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
    # 生成配音总开关（处理前选择，随工程持久化）。False = 仅字幕模式：管线跳过
    # 分离/克隆/合成，导出时原声比特级直通。旧工程没有该字段 → True（向后兼容）。
    enable_dubbing: bool = True
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
    # "该有而没有"的清单：翻译失败的行 / 没有配音的行。落盘是为了让界面和
    # 下一次运行都能看到缺口，而不是只活在这一次的日志里。
    failed_translation_ids: list[str] = field(default_factory=list)
    failed_audio_ids: list[str] = field(default_factory=list)
    # 产物是用哪套设置做出来的（见 pipeline_service._fingerprint）：换语言/口音/
    # 增强开关后，靠它判断该作废译文还是只作废配音。
    translation_fingerprint: str = ""
    tts_fingerprint: str = ""
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

def video_dir_path(video_id: str) -> str:
    """
    工程的目录路径，**不创建任何东西**。

    只读路径（播放音频、看状态、下载导出）必须用它：以前它们调 get_video_dir，
    而那个函数无条件 makedirs —— 于是任意一个 GET 就能在磁盘上创建一个目录
    （配合未校验的 video_id 就是"任意目录创建"）。
    """
    return os.path.join(config.DATA_DIR, video_id)


def get_video_dir(video_id: str) -> str:
    """工程的目录路径，并确保它存在（写方用）。"""
    video_dir = video_dir_path(video_id)
    os.makedirs(video_dir, exist_ok=True)
    return video_dir


def _atomic_write_json(path: str, data) -> None:
    """
    写 JSON：先写同目录的 `.part`，再 `os.replace` 覆盖目标。

    `os.replace` 在同一文件系统内是原子的，所以任何时刻读者看到的要么是旧文件、
    要么是新文件，不会是"写了一半"的半个 JSON。

    代价说清楚：进程在 `json.dump` 中途被杀时，会留下一个 `<name>.part`（本次
    写入没走到 replace）。它是纯残留 —— 没有任何代码读它，下一次写入会覆盖同名
    的它；最多每个产物一个，不会累积。以前这里直接以 "w" 覆盖：
    进程在写的中途被杀（部署重启、OOM、Ctrl-C）→ info.json 变成坏 JSON →
    `get_state()` 返回 None → 整个工程变成 404，用户看到"工程没了"（P1 #6）。
    stems 上传与 voice map 早就用了这个手法，最要紧的 info.json 反而没有。
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.part"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


# 这几个字段只由各自的专用端点写（字幕样式面板 / 导出面板），管线不碰它们。
# 但管线手里是一份"开跑时读入"的 state 副本，整段运行里会 `save_state` 很多次 ——
# 若照单全写，运行期间用户改的字幕样式会被副本里的旧值抹掉（lost update）。
# 所以 `save_state` 对这几个字段**以磁盘为准**，要改它们必须走 `update_state_fields`。
_USER_OWNED_FIELDS = ("subtitle_style", "subtitle_export")

# 与下面几个读—改—写文件共用的锁，定义在前面以便 save_state 使用。
_file_lock = threading.RLock()


def save_state(state: VideoState):
    """Save video state to the filesystem (atomically)."""
    video_id = state.video_id
    video_dir = get_video_dir(video_id)
    state_path = os.path.join(video_dir, "info.json")

    # Save root info (metadata only, no segments to avoid redundancy)
    data = state.to_dict()
    data.pop("segments", None)

    with _file_lock:
        existing = _read_json(state_path, {})
        if isinstance(existing, dict):
            for key in _USER_OWNED_FIELDS:
                if key in existing:
                    data[key] = existing[key]
        _atomic_write_json(state_path, data)
    logger.info(f"Saved metadata to {state_path}")


def update_state_fields(video_id: str, **fields) -> None:
    """
    只改 info.json 里指定的字段（读—改—写，整体原子）。

    用于 `_USER_OWNED_FIELDS`：它们不能经由 `save_state` 写，否则会和管线手里的
    旧副本互相覆盖。info.json 不存在（工程已被删）时抛 FileNotFoundError。
    """
    state_path = os.path.join(video_dir_path(video_id), "info.json")
    with _file_lock:
        existing = _read_json(state_path, None)
        if not isinstance(existing, dict):
            raise FileNotFoundError(state_path)
        existing.update(fields)
        _atomic_write_json(state_path, existing)


SEGMENT_FLAGS_FILENAME = "segment_flags.json"

# 这几个文件都是"读—改—写"：多个请求线程（同步端点跑在线程池里）同时改会互相
# 覆盖，共用上面的 `_file_lock`（进程内一把锁就够 —— 部署是单 worker）。


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def set_segment_flag(video_id: str, segment_id: str, flag: str, value: bool) -> None:
    """
    Persist ONE boolean flag for one segment ("muted" | "hidden").

    The whole dict is read and written back on purpose: the file holds a list
    per flag, and writing only the key being changed would silently drop the
    other one (mute a line, hide another, and the first list disappears).
    """
    video_dir = get_video_dir(video_id)
    path = os.path.join(video_dir, SEGMENT_FLAGS_FILENAME)

    with _file_lock:
        data = _read_json(path, {})
        if not isinstance(data, dict):
            data = {}
        ids = [i for i in (data.get(flag) or []) if i != segment_id]
        if value:
            ids.append(segment_id)
        data[flag] = ids
        _atomic_write_json(path, data)


TRANSLATION_FILENAME = "translation_result.json"
ASR_FILENAME = "asr_result.json"


def save_translations(video_id: str, segments: list["Segment"]) -> None:
    """
    把**整个工程**每一行的译文写进 translation_result.json。

    这个文件是"全片译文"的唯一记录，所以写入方必须传完整的 segments（从 state
    来），而不是某次请求里的几行：以前单行重译把只含这一行的结果写进来，刷新页面
    后其余行的译文全部消失。没有译文的行不写（与 load_state 的读取对应）。
    """
    path = os.path.join(get_video_dir(video_id), TRANSLATION_FILENAME)
    payload = [
        {"id": seg.id, "translatedText": seg.translated_text}
        for seg in segments
        if seg.translated_text
    ]
    with _file_lock:
        _atomic_write_json(path, payload)


def patch_asr_text(video_id: str, segment_id: str, text: str) -> bool:
    """
    把用户在编辑器里改过的**原文**写回 asr_result.json。返回是否找到这一行。

    asr_result.json 是原文的唯一落盘处（`save_state` 不存 segments），不写回的话
    刷新页面改动就丢了，而字幕、续跑、重译都会读回旧文本。
    """
    path = os.path.join(get_video_dir(video_id), ASR_FILENAME)
    with _file_lock:
        data = _read_json(path, None)
        if not isinstance(data, list):
            return False
        found = False
        for item in data:
            if isinstance(item, dict) and item.get("id") == segment_id:
                item["text"] = text
                found = True
                break
        if found:
            _atomic_write_json(path, data)
        return found


def speakers_from_segments(segments: list["Segment"]) -> list[Speaker]:
    """分段里出现过的说话人：id 是 ASR 的 speaker_id，name 是展示标签。"""
    labels: dict[str, str] = {}
    for seg in segments:
        labels.setdefault(seg.speaker_id, seg.speaker_label or seg.speaker_id)
    return [Speaker(id=sid, name=labels[sid]) for sid in sorted(labels)]


ENHANCED_FILENAME = "enhanced_transcript.json"


def drop_enhanced_line(video_id: str, segment_id: str) -> None:
    """
    把一行从 omni 审听缓存里去掉。

    用户手改了某行原文之后，缓存里那一行（按**旧**原文纠错/打标签的版本）就过期了：
    留着的话，随后的行级重译会把它当"审听版"套上去，把用户刚改的文字顶掉。
    """
    path = os.path.join(video_dir_path(video_id), ENHANCED_FILENAME)
    with _file_lock:
        data = _read_json(path, None)
        lines = data.get("lines") if isinstance(data, dict) else None
        if not isinstance(lines, dict) or segment_id not in lines:
            return
        lines.pop(segment_id)
        _atomic_write_json(path, data)


def _normalise_speakers(state: "VideoState") -> None:
    """
    说话人 id 统一成分段的 `speaker_id`（ASR 给的 "0"、"1"…）。

    老工程里 `Speaker.id` 存的是展示标签（"Speaker 0"），而音色映射、克隆映射、
    管线合成全按 `speaker_id` 查 —— 于是界面上钉音色、试听克隆音色都对不上号。
    这里把旧数据就地迁到新约定：id=speaker_id，name=展示标签。
    """
    if not state.segments:
        return
    by_label = {s.speaker_label: s.speaker_id for s in state.segments}
    ids = {s.speaker_id for s in state.segments}
    names = {s.speaker_id: s.speaker_label for s in state.segments}
    fixed: list[Speaker] = []
    seen: set[str] = set()
    for sp in state.speakers:
        sid = sp.id if sp.id in ids else by_label.get(sp.id, sp.id)
        if sid in seen:
            continue
        seen.add(sid)
        fixed.append(Speaker(id=sid, name=sp.name if sp.name != sp.id else names.get(sid, sp.name)))
    state.speakers = fixed


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
    
    # 1. info.json —— 只有它坏掉才算"这个工程读不出来"（返回 None＝404）。
    #    其余四个衍生文件坏了只降级：丢掉那一部分信息，工程本身照常打开。
    #    以前整段共用一个 try/except：任何一个衍生 JSON 坏掉，状态直接变 None，
    #    用户看到的是"工程没了"，连从断点续跑的机会都没有（P1 #6）。
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(
            f"[{video_id}] info.json is unreadable ({e}) — the project cannot be "
            f"opened. The file is still on disk: {info_path}"
        )
        return None

    try:
        # 2. Load ASR segments if they exist
        segments = []
        if os.path.exists(asr_path):
            try:
                with open(asr_path, "r", encoding="utf-8") as f:
                    asr_data = json.load(f)
                    segments = [Segment.from_dict(s) for s in asr_data]
            except Exception as e:
                # 只是"分段读不出来"，工程本身还在：不要让它变成 404。
                logger.warning(
                    f"[{video_id}] asr_result.json is unreadable ({e}); "
                    f"opening the project without its segments"
                )

        # 3. Load Translations and merge
        if segments and os.path.exists(trans_path):
            try:
                with open(trans_path, "r", encoding="utf-8") as f:
                    trans_data = json.load(f)
                    trans_map = {item["id"]: item["translatedText"] for item in trans_data}
                    for s in segments:
                        if s.id in trans_map:
                            s.translated_text = trans_map[s.id]
            except Exception as e:
                logger.warning(
                    f"[{video_id}] translation_result.json is unreadable ({e}); "
                    f"the lines will show without translations"
                )

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
                    entry = tts_map[s.id]
                    if isinstance(entry, dict):
                        # 新形状：{"path", "duration"} —— 时长是混音计划算 atempo
                        # 的输入，必须跟路径一起活过重启。
                        s.audio_path = os.path.join(video_dir, entry["path"])
                        if entry.get("duration"):
                            s.actual_duration = float(entry["duration"])
                    else:
                        # 旧形状：纯路径字符串
                        s.audio_path = os.path.join(video_dir, entry)
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
        state = VideoState.from_dict(data)
        _normalise_speakers(state)
        return state

    except Exception as e:
        # 走到这里说明是上面没预料到的异常。**仍然不要把工程判成不存在** ——
        # info.json 完好就说明工程在，只是衍生数据没读全（P1 #6）。
        logger.error(
            f"[{video_id}] failed to merge derived artifacts ({e}); "
            f"opening the project with metadata only"
        )
        try:
            data["segments"] = []
            return VideoState.from_dict(data)
        except Exception as e2:
            logger.error(f"[{video_id}] even the metadata could not be parsed: {e2}")
            return None


# Statuses that only exist WHILE a pipeline is running. Seeing one at startup, when
# no pipeline can be running, means the previous process died mid-run.
_IN_FLIGHT_STATUSES = {
    VideoStatus.EXTRACTING_AUDIO,
    VideoStatus.TRANSCRIBING,
    VideoStatus.TRANSLATING,
    VideoStatus.SYNTHESIZING,
}


def repair_interrupted_states() -> int:
    """
    Turn "in flight" statuses left behind by a dead process into CANCELLED.

    CANCELLED already means "resumable from the last finished phase", which is
    exactly the truth: every stage writes its artifact as it completes. Only
    `info.json` is touched, and only the status field. Returns how many projects
    were repaired.
    """
    if not os.path.isdir(config.DATA_DIR):
        return 0
    repaired = 0
    for name in os.listdir(config.DATA_DIR):
        info_path = os.path.join(config.DATA_DIR, name, "info.json")
        if not os.path.isfile(info_path):
            continue
        with _file_lock:
            data = _read_json(info_path, None)
            if not isinstance(data, dict):
                continue
            try:
                status = VideoStatus(data.get("status"))
            except ValueError:
                continue
            if status not in _IN_FLIGHT_STATUSES:
                continue
            data["status"] = VideoStatus.CANCELLED.value
            data.setdefault("cancelled_step", None)
            try:
                _atomic_write_json(info_path, data)
                repaired += 1
            except OSError as e:
                logger.warning(f"[{name}] could not repair interrupted status: {e}")
    return repaired


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
