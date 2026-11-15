"""
音轨上的"有声音但没有台词覆盖"的区间 —— 候选。

要找的是 ASR 漏掉的声音：笑、叹息、咳嗽、呼吸，或者被吞掉的话。用**能量**判断，不用 VAD 模型：
这类声音恰恰很轻，Silero 在样本上会漏掉最轻的叹息（0 到 42%），能量检测 4 个全部命中。
代价是怕背景残留，所以只在人声轨上有意义；没做过人声分离时，返回里会带说明。

关键是"减掉台词"：先把音轨上有声音的帧标出来，再把所有行（任何 kind）占的时间从中抠掉，
剩下的才是候选。以前把整段有声区间当候选，一个笑声紧贴句尾时会和那句话连成一块，边界就不对了。

边界是确定的（帧级，20ms），不靠模型估计。
"""
import os
import wave
from typing import Optional

import numpy as np

from app.models import asr_audio_path, vocals_audio_path

HOP_S = 0.02                # 帧长
MARGIN_DB = 12.0            # 高出底噪多少 dB 算"有声"
FLOOR_PERCENTILE = 10       # 底噪取整条轨的第 10 百分位
MIN_ON_S = 0.10             # 短于此的声音（咔哒、爆音）不算
MERGE_GAP_S = 0.15          # 间隔短于此的两段并成一段
MIN_CANDIDATE_S = 0.30      # 抠掉台词之后，剩下不足此长度的不算候选
LINE_PAD_S = 0.05           # 台词前后各多抠一点：ASR 的边界有几十毫秒的误差

_CACHE: dict = {}           # (路径, mtime, 大小) -> 每帧是否有声


class AudioError(Exception):
    pass


def _frame_flags(path: str) -> np.ndarray:
    """每 20ms 一帧，有声为 True。按块读，内存不随音频时长增长（2 小时的 wav 也只占几十 KB 的帧表）。"""
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2:
                raise AudioError(f"只支持 16 位 PCM wav（这个是 {w.getsampwidth() * 8} 位）")
            rate, channels = w.getframerate(), w.getnchannels()
            hop = max(1, int(rate * HOP_S))
            block_frames = hop * 3000                          # 约 60 秒一块
            parts = []
            while True:
                raw = w.readframes(block_frames)
                if not raw:
                    break
                x = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
                if channels > 1:
                    x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
                usable = len(x) // hop * hop
                if usable:
                    parts.append(np.sqrt((x[:usable].reshape(-1, hop) ** 2).mean(axis=1)))
                # 不足一帧的尾巴丢掉：最多 20ms，对"有没有声"没有影响
    except (wave.Error, EOFError, OSError) as e:
        raise AudioError(f"读不了音轨：{e}")
    if not parts:
        raise AudioError("音轨是空的")
    db = 20 * np.log10(np.concatenate(parts) + 1e-9)
    flags = db > np.percentile(db, FLOOR_PERCENTILE) + MARGIN_DB
    if len(_CACHE) >= 4:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = flags
    return flags


def _runs(mask: np.ndarray, hop: float = HOP_S) -> list[list[float]]:
    padded = np.concatenate([[False], mask, [False]]).astype(np.int8)
    edges = np.flatnonzero(np.diff(padded))
    return [[float(a) * hop, float(b) * hop] for a, b in zip(edges[0::2], edges[1::2])]


def _clean(runs: list[list[float]], min_on: float, merge_gap: float) -> list[list[float]]:
    merged: list[list[float]] = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < merge_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    return [r for r in merged if r[1] - r[0] >= min_on]


def _track(video_id: str) -> Optional[str]:
    return asr_audio_path(video_id)


def active_fraction(video_id: str, start: float, end: float) -> Optional[float]:
    """[start, end] 里有声的帧占多少（0~1）。没有音轨 / 读不了返回 None —— 调用方应当"没有证据就不拦"。"""
    path = _track(video_id)
    if not path or end <= start:
        return None
    try:
        flags = _frame_flags(path)
    except AudioError:
        return None
    a, b = int(max(0.0, start) / HOP_S), int(max(0.0, end) / HOP_S)
    window = flags[a:max(a + 1, b)]
    return float(window.mean()) if len(window) else None


def audio_seconds(video_id: str) -> Optional[float]:
    path = _track(video_id)
    if not path:
        return None
    try:
        return len(_frame_flags(path)) * HOP_S
    except AudioError:
        return None


def find_candidates(video_id: str, segments) -> Optional[list[dict]]:
    """
    有声音、但没被任何一行覆盖的区间：[{start, end, after_line, after_id}]。

    `after_line`：这个区间前面最近的一行的行号（1 起；0 = 在第一行之前）—— 给人和 AI 用的位置。
    没有音轨 / 读不了返回 None（和"没有候选"的空列表区分开）。
    """
    path = _track(video_id)
    if not path:
        return None
    try:
        flags = _frame_flags(path)
    except AudioError:
        return None
    mask = flags.copy()
    for seg in segments:
        a = int(max(0.0, seg.start_time - LINE_PAD_S) / HOP_S)
        b = int(max(0.0, seg.end_time + LINE_PAD_S) / HOP_S) + 1
        mask[a:b] = False
    runs = _clean(_runs(mask), MIN_ON_S, MERGE_GAP_S)
    ordered = sorted(enumerate(segments, 1), key=lambda p: p[1].end_time)
    out = []
    for s, e in runs:
        if e - s < MIN_CANDIDATE_S:
            continue
        before = [(i, g) for i, g in ordered if g.end_time <= s + LINE_PAD_S + 1e-6]
        after_line, after_seg = before[-1] if before else (0, None)
        out.append({"start": round(float(s), 2), "end": round(float(e), 2), "after_line": after_line,
                    "after_id": after_seg.id if after_seg else None})
    return out


FOCUS_PAD_S = 0.3           # 聚焦判断时，时间窗前后最多多留这么多


def focus_window(video_id: str, segments, seg, pad: float = FOCUS_PAD_S) -> tuple[float, float]:
    """
    给"只对这一行再判断一次"剪音频用的时间窗：这一行前后各留一点余量，**碰到邻居就停**。

    为什么要留余量：被 ASR 吞掉的那句话往往比这一行多说一点（样本里 B 的反驳比这一行晚 0.15 秒结束），
    严格按 ASR 的时间窗剪会把它的尾巴剪掉（找回被吞的话 5/5 → 2/4）。
    为什么碰到邻居要停：余量会把紧挨着的另一句台词、或者候选声音（笑声）的头几百毫秒剪进来，
    模型会把它当成"第二个声音"（单人说话的对照行误报 0/8 → 2/4）。

    邻居 = 别的行的边界、以及上面算出的"没被台词覆盖的声音"的边界。两者都是确定的。
    """
    lo, hi = max(0.0, seg.start_time - pad), seg.end_time + pad
    eps = 1e-6
    edges: list[tuple[float, float]] = [(g.start_time, g.end_time) for g in segments if g.id != seg.id]
    edges += [(c["start"], c["end"]) for c in (find_candidates(video_id, segments) or [])]
    for a, b in edges:
        if b <= seg.start_time + eps:
            lo = max(lo, b)
        if a >= seg.end_time - eps:
            hi = min(hi, a)
    return min(lo, seg.start_time), max(hi, seg.end_time)


def is_separated(video_id: str) -> bool:
    """听的是不是人声轨。不是的话候选里会混着风声、音乐这类背景声。"""
    return vocals_audio_path(video_id) is not None
