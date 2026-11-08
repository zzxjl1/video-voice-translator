"""
项目文档的读写与变更 —— 整个后端**唯一**写状态的地方。

    state = store.get_state(video_id)                # 只读快照（深拷贝，已 hydrate）
    store.mutate(video_id, fn, actor="ai", ...)      # 唯一的写入口

`mutate` 在一把锁里做完全部事情：读最新文档 → 在副本上跑 `fn` → 没变化就什么都不做 →
变了就原子写盘、rev+1、算出客户端视图的增量、记下撤销信息、发出 `change` 事件。
`fn` 抛异常时副本被丢弃，文档不受影响。

这一个入口取代了旧版的 save_state / update_state_fields / save_translations /
patch_asr_text / set_segment_flag / drop_enhanced_line / tts 登记表 / 音色表 / 克隆表。
管线手里不再有"开跑时读入的一份旧副本"，也就没有 lost update。

撤销是通用的：不需要每个命令写逆操作，而是对"改前/改后"的文档做差，存下改前的值。
"""
import copy
import json
import logging
import os
import shutil
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app import status, view
from app.bus import bus
from app.models import (
    CHAT_FILE, EVENTS_FILE, PROJECT_FILE, Segment, Speaker, VideoState,
    atomic_write_json, get_video_dir, video_dir_path,
)

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 500          # 内存里保留（也是落盘后重读）的最近事件数
_COMPACT_BYTES = 6 * 1024 * 1024


class ProjectNotFound(Exception):
    pass


class NothingToUndo(Exception):
    pass


_global_lock = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_cache: dict[str, VideoState] = {}
_history: dict[str, deque] = {}


def lock_for(video_id: str) -> threading.RLock:
    with _global_lock:
        return _locks.setdefault(video_id, threading.RLock())


def project_path(video_id: str) -> str:
    return os.path.join(video_dir_path(video_id), PROJECT_FILE)


def exists(video_id: str) -> bool:
    return os.path.isfile(project_path(video_id))


def _write(state: VideoState) -> None:
    atomic_write_json(project_path(state.video_id), state.to_dict())


def _load_locked(video_id: str) -> Optional[VideoState]:
    cached = _cache.get(video_id)
    if cached is not None:
        return cached
    path = project_path(video_id)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = VideoState.from_dict(json.load(f))
    except Exception as e:
        logger.error(f"[{video_id}] project.json is unreadable ({e}); the file is still on disk: {path}")
        return None
    _cache[video_id] = state
    return state


def create(video_id: str, filename: str, file_path: str) -> VideoState:
    """新建工程；已存在则原样返回（同一个文件的 MD5 相同 = 同一个工程）。"""
    with lock_for(video_id):
        existing = _load_locked(video_id)
        if existing is not None:
            return copy.deepcopy(existing)
        get_video_dir(video_id)
        state = VideoState(video_id=video_id, filename=filename, file_path=file_path)
        _write(state)
        _cache[video_id] = state
        return copy.deepcopy(state)


def get_state(video_id: str) -> Optional[VideoState]:
    """只读快照：深拷贝并 hydrate（派生的 audio_path 已填好）。不存在返回 None。"""
    with lock_for(video_id):
        state = _load_locked(video_id)
        if state is None:
            return None
        snap = copy.deepcopy(state)
    return status.hydrate(snap)


def require_state(video_id: str) -> VideoState:
    state = get_state(video_id)
    if state is None:
        raise ProjectNotFound(video_id)
    return state


def drop_cache(video_id: str) -> None:
    """测试或外部改了 project.json 之后，让下次读取重新从磁盘来。"""
    with lock_for(video_id):
        _cache.pop(video_id, None)


# ----------------------------------------------------------------------------
# 事件落盘与历史
# ----------------------------------------------------------------------------

def _events_path(video_id: str) -> str:
    return os.path.join(video_dir_path(video_id), EVENTS_FILE)


def _hist(video_id: str) -> deque:
    """最近的落盘事件（change / job）。首次访问时从 events.jsonl 读回来。"""
    dq = _history.get(video_id)
    if dq is not None:
        return dq
    dq = deque(maxlen=HISTORY_LIMIT)
    path = _events_path(video_id)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            dq.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            if os.path.getsize(path) > _COMPACT_BYTES:
                with open(path + ".part", "w", encoding="utf-8") as f:
                    for event in dq:
                        f.write(json.dumps(event, ensure_ascii=False) + "\n")
                os.replace(path + ".part", path)
        except OSError as e:
            logger.warning(f"[{video_id}] could not read {EVENTS_FILE}: {e}")
    if dq:
        bus.seed(video_id, max(e.get("seq", 0) for e in dq))
    _history[video_id] = dq
    return dq


def _persist_event(video_id: str, record: dict) -> None:
    _hist(video_id).append(record)
    try:
        with open(_events_path(video_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning(f"[{video_id}] could not append to {EVENTS_FILE}: {e}")


def record_job_event(video_id: str, event: dict) -> dict:
    """任务的开始/结束：发出并落盘（AI 的"自上次以来发生了什么"要看得到它们）。"""
    with lock_for(video_id):
        _hist(video_id)                      # 先把 seq 对齐到磁盘上的最大值
        pub = bus.publish(video_id, {"type": "job", **event})
        _persist_event(video_id, dict(pub))
    return pub


def current_seq(video_id: str) -> int:
    """
    这个工程当前的事件序号。

    重启后内存里的序号是 0，要先把落盘事件读回来对齐（`_hist` 里做）才是对的 ——
    直接问 bus 会拿到 0，客户端据此订阅就会把整个历史重放一遍，按 seq 去重还会把
    旧事件当新事件。所以所有对外报告 seq 的地方都该走这里，而不是 `bus.current_seq`。
    """
    with lock_for(video_id):
        _hist(video_id)
        return bus.current_seq(video_id)


def events_since(video_id: str, seq: int, types: tuple = ("change", "job")) -> list[dict]:
    """落盘事件里 seq 之后的那些（给 AI 的"自上次以来"摘要用）。"""
    with lock_for(video_id):
        return [e for e in _hist(video_id) if e.get("seq", 0) > seq and e.get("type") in types]


# ----------------------------------------------------------------------------
# 变更
# ----------------------------------------------------------------------------

@dataclass
class Mutation:
    value: Any            # fn 的返回值
    changed: bool
    rev: int
    seq: Optional[int] = None


def mutate(
    video_id: str,
    fn: Callable[[VideoState], Any],
    *,
    actor: str = "system",
    cmd: str = "",
    summary: str = "",
    undoable: Optional[bool] = None,
    extra: Optional[dict] = None,
) -> Mutation:
    """
    唯一的写入口。`fn(state)` 就地修改传入的副本，返回值原样带回。

    `fn` 必须快（它在锁里）：慢的事（网络、ffmpeg）先做完，把结果拿进来再改文档。

    `undoable` 缺省：用户和 AI 的改动可撤销，系统（初始化出的转写、翻译、配音这些
    产物）不可撤销 —— 否则连按撤销就能把整份转写结果撤没。
    """
    if undoable is None:
        undoable = actor != "system"
    with lock_for(video_id):
        current = _load_locked(video_id)
        if current is None:
            raise ProjectNotFound(video_id)
        _hist(video_id)                              # 对齐 seq 起点

        before_dict = current.to_dict()
        before_view = view.snapshot(current)

        work = copy.deepcopy(current)
        value = fn(work)                             # 抛异常 → work 丢弃，文档不变
        after_dict = work.to_dict()
        if after_dict == before_dict:
            return Mutation(value, False, current.rev)

        work.rev = current.rev + 1
        after_dict["rev"] = work.rev
        _write(work)
        _cache[video_id] = work

        patch = view.diff(before_view, view.snapshot(work))
        seg_patch = patch.get("segments") or {}
        ids = [s["id"] for s in seg_patch.get("upsert", [])] + list(seg_patch.get("remove", []))

        record = {
            "type": "change", "actor": actor, "cmd": cmd, "summary": summary,
            "rev": work.rev, "ids": ids,
            "undoable": bool(undoable),
            **(extra or {}),
        }
        pub = bus.publish(video_id, {**record, "patch": patch})
        record["seq"], record["ts"] = pub["seq"], pub["ts"]
        record["undo"] = _undo_patch(before_dict, after_dict) if undoable else None
        _persist_event(video_id, record)
        return Mutation(value, True, work.rev, pub["seq"])


def _undo_patch(before: dict, after: dict) -> dict:
    """改前/改后的差 → 能把它退回去的最小补丁。对任何命令都成立，不需要逐个写逆操作。"""
    fields = {
        k: before[k] for k in before
        if k not in ("segments", "rev") and before[k] != after.get(k)
    }
    b_seg = {s["id"]: s for s in before["segments"]}
    a_seg = {s["id"]: s for s in after["segments"]}
    b_order = [s["id"] for s in before["segments"]]
    a_order = [s["id"] for s in after["segments"]]
    return {
        "fields": fields,
        "restore": {i: b_seg[i] for i in b_seg if a_seg.get(i) != b_seg[i]},
        "remove": [i for i in a_seg if i not in b_seg],
        "order": b_order if b_order != a_order else None,
    }


def _apply_undo(state: VideoState, undo: dict) -> None:
    for key, value in undo["fields"].items():
        value = copy.deepcopy(value)
        if key == "speakers":
            value = [Speaker.from_dict(s) for s in value]
        setattr(state, key, value)

    by_id = {s.id: s for s in state.segments}
    for sid in undo["remove"]:
        by_id.pop(sid, None)
    for sid, data in undo["restore"].items():
        by_id[sid] = Segment.from_dict(copy.deepcopy(data))

    if undo["order"] is not None:
        ordered = [by_id.pop(i) for i in undo["order"] if i in by_id]
        state.segments = ordered + list(by_id.values())      # 理论上为空：order 覆盖全部
    else:
        state.segments = [by_id[s.id] for s in state.segments if s.id in by_id]


def undo(video_id: str, actor: str = "user") -> dict:
    """撤销最近一次可撤销、且还没被撤销的改动。返回被撤销那条的摘要。"""
    with lock_for(video_id):
        history = _hist(video_id)
        already = {e["undoes"] for e in history if e.get("undoes")}
        target = next(
            (e for e in reversed(history)
             if e.get("type") == "change" and e.get("undo") and e["seq"] not in already),
            None,
        )
        if target is None:
            raise NothingToUndo()
        mutate(
            video_id, lambda s: _apply_undo(s, target["undo"]),
            actor=actor, cmd="undo", summary=f"撤销：{target['summary']}",
            undoable=False, extra={"undoes": target["seq"]},
        )
        return {"seq": target["seq"], "summary": target["summary"], "cmd": target["cmd"]}


def can_undo(video_id: str) -> Optional[dict]:
    with lock_for(video_id):
        history = _hist(video_id)
        already = {e["undoes"] for e in history if e.get("undoes")}
        target = next(
            (e for e in reversed(history)
             if e.get("type") == "change" and e.get("undo") and e["seq"] not in already),
            None,
        )
        return {"seq": target["seq"], "summary": target["summary"]} if target else None


# ----------------------------------------------------------------------------
# 聊天记录
# ----------------------------------------------------------------------------

def _chat_path(video_id: str) -> str:
    return os.path.join(video_dir_path(video_id), CHAT_FILE)


def append_chat(video_id: str, message: dict) -> dict:
    with lock_for(video_id):
        try:
            with open(_chat_path(video_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning(f"[{video_id}] could not append to {CHAT_FILE}: {e}")
    return message


def read_chat(video_id: str, limit: int = 200) -> list[dict]:
    """
    聊天记录，旧 → 新。

    文件是只追加的。确认卡片的"已确认/已拒绝"不改写原行，而是追加一条
    `{"role": "confirm_state", "ref": <卡片 id>, "status": ...}`，读取时折叠回那张卡片 ——
    和事件日志同一个思路：写入永远是追加（崩溃不会留下写了一半的文件），读取时还原最终状态。
    """
    path = _chat_path(video_id)
    if not os.path.isfile(path):
        return []
    rows: list[dict] = []
    with lock_for(video_id):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []
    states = {r["ref"]: r["status"] for r in rows if r.get("role") == "confirm_state"}
    out = []
    for r in rows:
        if r.get("role") == "confirm_state":
            continue
        if r.get("role") == "confirm" and r["id"] in states:
            r = {**r, "status": states[r["id"]]}
        out.append(r)
    return out[-limit:]


def resolve_confirm_card(video_id: str, card_id: str, approved: bool) -> None:
    append_chat(video_id, {"role": "confirm_state", "ref": card_id,
                           "status": "approved" if approved else "rejected"})


# ----------------------------------------------------------------------------
# 删除
# ----------------------------------------------------------------------------

def delete(video_id: str) -> None:
    with lock_for(video_id):
        _cache.pop(video_id, None)
        _history.pop(video_id, None)
        shutil.rmtree(video_dir_path(video_id), ignore_errors=True)
    bus.forget(video_id)
    with _global_lock:
        _locks.pop(video_id, None)


def reset_dir(video_id: str, keep: tuple = ()) -> None:
    """清空工程目录，只留 `keep` 里的文件（通常是原视频）。文档本身也要重建，由调用方负责。"""
    base = video_dir_path(video_id)
    if not os.path.isdir(base):
        return
    for name in os.listdir(base):
        if name in keep:
            continue
        path = os.path.join(base, name)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
