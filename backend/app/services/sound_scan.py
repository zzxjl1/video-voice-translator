"""
初始化时的"漏掉的非语言声"：人声轨上有声音、但没有任何一句台词覆盖的片段。

这是整条链路里**唯一**自动应用的一类，因为它满足两个条件：不需要模型（候选的边界是音轨能量定的，确定），
而且结论只有一种（人声轨上有声音但 ASR 没转写，多半是笑、叹气、咳嗽）。自动应用的结果是
`kind=vocal` 的行：不翻译、不配音、不出字幕，导出时把人声轨里这一段原声放回去（见 mix_service）。

不自动应用的（写进初始化报告，由聊天里的 AI 提示、人来判断）：
  · long      超过 AUTO_MAX_S 的片段 —— 更像被 ASR 漏掉的一句话，不是笑声；
  · too_many  数量或占比超出常理 —— 多半是这一段整体没识别好（比如整段外语、带歌词的音乐），
              全标成非语言声会把真正的台词藏起来；
  · no_stem   没做过人声分离 —— 候选里混着风声、背景音乐，而且原声本来就在垫底里，没有保留的意义。

两步：`classify`（纯函数，耗时的在这里，可以放线程里）→ `apply`（写状态，必须在事件循环线程里做）。
"""
import logging
import uuid
from typing import Optional

from app import store
from app.config import env_float, env_int
from app.models import Segment
from app.services import audio_energy, mix_service

logger = logging.getLogger(__name__)

LABEL = "（非语言声）"
AUTO_MAX_S = env_float("SOUNDS_AUTO_MAX_S", 4.0)                 # 自动标记的单段上限（笑声通常 1~3 秒）
AUTO_MAX_COUNT = env_int("SOUNDS_AUTO_MAX_COUNT", 20)            # 一个工程最多自动标这么多段
AUTO_MAX_FRACTION = env_float("SOUNDS_AUTO_MAX_FRACTION", 0.25)  # 没被台词覆盖的声音占全片超过这个比例就一概不自动
REPORT_MAX_PENDING = 20                                          # 报告里最多列出多少段待确认的

LONG, TOO_MANY, NO_STEM = "long", "too_many", "no_stem"


def classify(video_id: str, state) -> Optional[dict]:
    """
    把候选分成"自动标记"和"待确认"。不改任何东西。没有可分析的音轨时返回 None。

    {canKeep, auto: [候选], pending: [{**候选, reason}]}。候选字段见 audio_energy.find_candidates。
    """
    cands = audio_energy.find_candidates(video_id, state.segments)
    if cands is None:
        return None
    # 能保留原声才有自动标记的意义：要有人声轨（候选才是"人声里"的声音）和导出用的全频带人声 + 背景轨
    can_keep = audio_energy.is_separated(video_id) and mix_service.keep_available(video_id)
    total = audio_energy.audio_seconds(video_id) or 0.0
    uncovered = sum(c["end"] - c["start"] for c in cands)

    blanket: Optional[str] = None
    if not can_keep:
        blanket = NO_STEM
    elif len(cands) > AUTO_MAX_COUNT or (total > 0 and uncovered > AUTO_MAX_FRACTION * total):
        blanket = TOO_MANY

    auto, pending = [], []
    for c in cands:
        if blanket:
            pending.append({**c, "reason": blanket})
        elif c["end"] - c["start"] > AUTO_MAX_S:
            pending.append({**c, "reason": LONG})
        else:
            auto.append(c)
    return {"canKeep": can_keep, "auto": auto, "pending": pending}


def _new_id() -> str:
    return f"seg-{uuid.uuid4().hex[:8]}"


def _insert(state, auto: list) -> list[str]:
    """就地把候选插成 vocal 行，返回新行 id。位置按候选的 after_line（和 insert_line 同一个坐标）。"""
    original = list(state.segments)
    ids: list[str] = []
    # 从后往前插：前面的插入不会让后面候选的下标失效；同一个位置的两段，先插后面的，顺序就对
    for c in sorted(auto, key=lambda c: c["start"], reverse=True):
        k = c["after_line"]
        if 0 < k <= len(original):
            speaker_id = original[k - 1].speaker_id          # 非语言声没有说话人：借前一句的，只影响时间线颜色
        else:
            speaker_id = state.speakers[0].id if state.speakers else "0"
        seg = Segment(id=_new_id(), speaker_id=speaker_id, start_time=c["start"], end_time=c["end"],
                      text=LABEL, kind="vocal")
        state.segments.insert(k, seg)
        ids.append(seg.id)
    return ids


def apply(video_id: str, plan: Optional[dict]) -> dict:
    """
    执行 `classify` 的结果，返回写进 init_report["sounds"] 的小结。

    只在这个工程还没扫描过（`sounds_scanned`）时才插行：用户撤销、删掉或标回语音之后，再跑初始化
    （补齐、改设置）不能把他刚否掉的又加回来。扫描标记**先于**插入写入，而且不可撤销：撤销补丁里包含
    所有变化的顶层字段，标记和插入放在同一次改动里的话，撤销就会把标记也退回 False。
    """
    state = store.require_state(video_id)
    if plan is None:
        return {"track": "none", "applied": [], "pendingCount": 0, "pending": []}

    applied_ids: list[str] = []
    if plan["canKeep"] and not state.sounds_scanned:
        def mark(s):
            s.sounds_scanned = True
        store.mutate(video_id, mark, actor="system", cmd="sounds_scanned", summary="已扫描非语言声",
                     undoable=False, silent=True)
        if plan["auto"]:
            def insert(s):
                fresh = classify(video_id, s)          # 在最新的状态上重算：期间有人改过行也不会插错位
                if fresh and fresh["auto"]:
                    applied_ids.extend(_insert(s, fresh["auto"]))
            store.mutate(video_id, insert, actor="system", cmd="auto_sounds", undoable=True,
                         summary=f"自动标出 {len(plan['auto'])} 处人声里的非语言声")

    return summarize(video_id, applied_ids)


def summarize(video_id: str, applied_ids: list[str]) -> dict:
    """应用之后的小结：已标记的（带 id，欢迎消息按当前行号找）和仍待确认的。"""
    state = store.require_state(video_id)
    by_id = {s.id: s for s in state.segments}
    post = classify(video_id, state) or {"canKeep": False, "pending": []}
    return {
        "track": "vocals" if audio_energy.is_separated(video_id) else "original",
        "applied": [{"id": i, "start": round(by_id[i].start_time, 2), "end": round(by_id[i].end_time, 2)}
                    for i in applied_ids if i in by_id],
        "pendingCount": len(post["pending"]),
        "pending": [{"afterId": c["after_id"], "start": c["start"], "end": c["end"], "reason": c["reason"]}
                    for c in post["pending"][:REPORT_MAX_PENDING]],
    }
