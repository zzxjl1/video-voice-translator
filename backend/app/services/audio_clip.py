"""
音频片段：从一条音轨里切出一段时间窗。

两个调用方，要求一致：
  · 预览：`GET /projects/{id}/keep/{segment}` 把保留的原声片段送给浏览器；
  · 导出：混音时间轴把同一段原声摆进去。
所以只有一个实现，两边听到的是同一段声音。

用 ffmpeg 输出裸 PCM，wav 头由 `wave` 自己写。让 ffmpeg 往管道里写 wav 的话，它没法回头改
文件头，头里的长度是个巨大的占位值（实测解码出 13 万秒），下游会读错。
"""
import io
import subprocess
import wave


class ClipError(Exception):
    """切不出来：时间窗不合法、文件读不了。消息直接给人看。"""


def decode_clip_pcm(path: str, start: float, end: float, sample_rate: int) -> bytes:
    """`path` 里 [start, end) 秒，解码成单声道 s16le 裸 PCM（任何输入格式、任何采样率都行）。"""
    if end <= start:
        raise ClipError("时间窗的终点必须大于起点")
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, start):.4f}", "-t", f"{end - max(0.0, start):.4f}",
           "-i", path, "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(sample_rate), "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ClipError(f"没能读出音频：{e}")
    if r.returncode != 0:
        raise ClipError(f"音频读不出来：{r.stderr.decode('utf-8', errors='replace')[-200:]}")
    if not r.stdout:
        raise ClipError("这段时间超出了音频长度")
    return r.stdout


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(pcm)
    return buf.getvalue()


def clip_wav_bytes(path: str, start: float, end: float, sample_rate: int) -> bytes:
    return pcm_to_wav(decode_clip_pcm(path, start, end, sample_rate), sample_rate)
