"""
响度测量：配音要还原到原片这一句人声的响度。

比较的是**同一种东西**：只统计"有人在发声"的那些帧的平均功率（去掉静音和句间停顿）。TTS 的 mp3 首尾常带静音、
原片人声轨一句里也有停顿，直接算整段 RMS 会被静音比例带偏。

只在合成配音时量一次，结果存在行上（`Segment.tts_gain_db`），预览和导出读同一个数，不在每次出计划时重新量音频。
"""
import math
import subprocess
from typing import Optional

import numpy as np

from app.services import audio_clip

RATE = 16000
FRAME_S = 0.02
GATE_DB = 30.0          # 比最响的那一帧低多少 dB 以内的帧才算"在发声"
MIN_ACTIVE_S = 0.15     # 有声部分短于这个，量出来的数不可靠（一声咳嗽）
MAX_GAIN_DB = 12.0      # 单句最多调这么多：差得更多说明参考本身有问题（串了别人的声音、爆音），不去追
MIN_GAIN_DB = -12.0


def level_db(x: np.ndarray, rate: int = RATE) -> Optional[float]:
    """有声帧的平均功率（dBFS）。没有足够的有声内容返回 None。"""
    n = int(rate * FRAME_S)
    if n <= 0 or len(x) < n:
        return None
    frames = x[: len(x) // n * n].reshape(-1, n)
    power = np.mean(frames.astype(np.float64) ** 2, axis=1)
    peak = power.max()
    if peak <= 1e-12:
        return None
    active = power >= peak * 10 ** (-GATE_DB / 10)
    if active.sum() * FRAME_S < MIN_ACTIVE_S:
        return None
    return float(10 * math.log10(power[active].mean()))


def decode_file(path: str, rate: int = RATE) -> np.ndarray:
    """任意音频文件 → 单声道 float 样本。"""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        capture_output=True, timeout=60,
    )
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("读不出音频")
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float64) / 32768.0


def decode_window(path: str, start: float, end: float, rate: int = RATE) -> np.ndarray:
    pcm = audio_clip.decode_clip_pcm(path, start, end, rate)
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float64) / 32768.0


def align_gain_db(tts_path: str, reference_path: str, start: float, end: float) -> Optional[float]:
    """
    这一句配音要加多少 dB，才和原片这一句的人声一样响。

    测不出（原片这一段几乎没有人声、或配音是空的）就返回 None —— 不动它，而不是猜一个数。
    夹在 ±MAX_GAIN_DB：差得离谱时多半是参考有问题，追过去反而更糟。
    """
    try:
        want = level_db(decode_window(reference_path, start, end))
        have = level_db(decode_file(tts_path))
    except (RuntimeError, audio_clip.ClipError, OSError, subprocess.TimeoutExpired):
        return None
    if want is None or have is None:
        return None
    return round(max(MIN_GAIN_DB, min(MAX_GAIN_DB, want - have)), 1)


def to_linear(db: float) -> float:
    return round(10 ** (db / 20.0), 4)
