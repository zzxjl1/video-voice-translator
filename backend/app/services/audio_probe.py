"""
MP3 时长：直接读帧头，不起进程。

为什么有这个模块
----------------
TTS 每合成一句就要量一次时长（拟合语速），导出和 /status 还要再量。每次起一个
`ffprobe` 进程在低配机器上是实打实的成本，而 TTS 的产物恰好是最容易自己解析的
那一类：一串干净的 MPEG 音频帧。

契约：**返回时长（秒），或者在没有十足把握时返回 None** —— None 的意思是"去问
ffprobe"，这个模块从不猜。它逐帧累加每帧携带的采样数，所以填充、VBR、帧间码率
变化都是精确的（`文件大小 / 码率` 做不到这一点）。

刻意拒答（→ None → ffprobe）的情况：
  · 带 Xing / Info / VBRI 头的文件：时长取决于编码器延迟/补零字段，各解码器的
    裁法不一致，没有哪个数字是"标准答案"；
  · 自由格式码率、中途换采样率/声道/版本、失去帧同步、末尾帧被截断；
  · 不是 MP3（WAV、随机字节、空文件……）。

验证：`tests/test_audio_probe.py` 把结果与 ffmpeg **实际解码出的采样数**逐个对照
（不是对照 ffprobe —— 无 Xing 头的 VBR 文件上 ffprobe 自己是估算，会偏几百毫秒）。
"""
from __future__ import annotations

import os

# 码率表（kbps），按 4 位索引取；索引 0 = 自由格式，15 = 非法。键：(MPEG 版本组, 层)。
_BITRATES = {
    ("1", 1): (0, 32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448, 0),
    ("1", 2): (0, 32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384, 0),
    ("1", 3): (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0),
    ("2", 1): (0, 32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256, 0),
    ("2", 2): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
    ("2", 3): (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0),
}
_SAMPLE_RATES = {"1": (44100, 48000, 32000), "2": (22050, 24000, 16000), "2.5": (11025, 12000, 8000)}

# 超过这个大小的文件不是 TTS 片段：不读进内存，直接交给 ffprobe。
_MAX_BYTES = 32 * 1024 * 1024
# 音频结束后允许的尾巴（ID3v1 之外）：APEv2 或零填充。更长的"尾巴"说明中途丢了同步。
_MAX_TRAILER = 64 * 1024
# 首帧里要找 Xing/Info/VBRI 的范围。三者都在帧头后 36~40 字节内，64 足够并留有余量。
_MARKER_SCAN = 64
_MARKERS = (b"Xing", b"Info", b"VBRI")


class _Frame:
    __slots__ = ("length", "samples", "rate", "channels", "version", "layer")

    def __init__(self, length, samples, rate, channels, version, layer):
        self.length, self.samples, self.rate = length, samples, rate
        self.channels, self.version, self.layer = channels, version, layer

    def stream_key(self):
        return (self.rate, self.channels, self.version, self.layer)


def _parse_header(data: bytes, pos: int):
    """data[pos:pos+4] 若是合法的 MPEG 音频帧头，返回 _Frame，否则 None。"""
    if pos + 4 > len(data):
        return None
    b0, b1, b2, b3 = data[pos], data[pos + 1], data[pos + 2], data[pos + 3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_bits, layer_bits = (b1 >> 3) & 3, (b1 >> 1) & 3
    if version_bits == 1 or layer_bits == 0:                      # 保留值
        return None
    version = {3: "1", 2: "2", 0: "2.5"}[version_bits]
    layer = {3: 1, 2: 2, 1: 3}[layer_bits]
    bitrate_index, rate_index, padding = b2 >> 4, (b2 >> 2) & 3, (b2 >> 1) & 1
    if bitrate_index in (0, 15) or rate_index == 3:               # 自由格式 / 非法 / 保留
        return None
    kbps = _BITRATES[("1" if version == "1" else "2", layer)][bitrate_index]
    rate = _SAMPLE_RATES[version][rate_index]
    channels = 1 if (b3 >> 6) == 3 else 2
    if layer == 1:
        samples = 384
        length = (12 * kbps * 1000 // rate + padding) * 4
    else:
        samples = 1152 if (layer == 2 or version == "1") else 576
        length = (samples // 8) * kbps * 1000 // rate + padding
    if length < 4:
        return None
    return _Frame(length, samples, rate, channels, version, layer)


def _id3v2_size(data: bytes) -> int:
    """开头 ID3v2 标签占的总字节数（没有则 0）。"""
    if len(data) < 10 or data[:3] != b"ID3":
        return 0
    size_bytes = data[6:10]
    if any(b & 0x80 for b in size_bytes):                         # sync-safe 整数不会置最高位
        return 0
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    return 10 + size + (10 if data[5] & 0x10 else 0)              # 有 footer 标志再加 10


def _duration_of(data: bytes) -> float | None:
    pos = _id3v2_size(data)
    end = len(data)
    if end >= 128 and data[end - 128:end - 125] == b"TAG":        # 末尾 ID3v1
        end -= 128

    first = _parse_header(data, pos)
    if first is None or first.layer != 3:
        return None
    if any(m in data[pos + 4:pos + min(first.length, _MARKER_SCAN)] for m in _MARKERS):
        return None

    key = first.stream_key()
    total = 0
    frames = 0
    while pos + 4 <= end:
        frame = _parse_header(data, pos)
        if frame is None:
            break
        if frame.stream_key() != key:
            return None                                           # 中途换了流参数
        if pos + frame.length > end:
            return None                                           # 末帧被截断：各解码器的处理不一致
        total += frame.samples
        frames += 1
        pos += frame.length

    if frames == 0:
        return None
    tail = data[pos:end]
    if tail:
        if len(tail) > _MAX_TRAILER:
            return None                                           # 在音频中间丢了同步
        if tail.strip(b"\x00") and tail[:8] != b"APETAGEX":
            return None
    return total / first.rate


def mp3_duration(path: str) -> float | None:
    """MP3 文件的时长（秒）；没有十足把握（或读不了）就返回 None，由调用方去问 ffprobe。"""
    try:
        size = os.path.getsize(path)
        if size < 4 or size > _MAX_BYTES:
            return None
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return _duration_of(data)
