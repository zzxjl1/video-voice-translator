"""
audio_probe.mp3_duration：与 ffmpeg **实际解码出的采样数**对照（不是对照 ffprobe ——
无 Xing 头的 VBR 文件上 ffprobe 自己是估算，会偏几百毫秒），并验证"没把握就拒答"。

需要 ffmpeg（用 libmp3lame 现场生成各种 MP3）。
"""
import os, random, subprocess, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
from app.services import subtitle_service
from app.services.audio_probe import mp3_duration

TMP = tempfile.mkdtemp()
fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond: fails.append(name)

def run(cmd):
    r = subprocess.run(cmd, capture_output=True)
    assert r.returncode == 0, r.stderr[-300:]
    return r.stdout

def synth(name, dur, rate, channels, args=(), src="sine=f=300"):
    path = f"{TMP}/{name}.mp3"
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"{src}:d={dur}:r={rate}",
         "-ac", str(channels), "-c:a", "libmp3lame", *args, path])
    return path

def decoded_seconds(path):
    """ground truth：ffmpeg 真正产出多少采样（按文件自己的采样率）"""
    rate = int(run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                    "stream=sample_rate", "-of", "default=nw=1:nk=1", path]).decode().strip())
    pcm = run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-acodec", "pcm_s16le",
               "-ac", "1", "-ar", str(rate), "-"])
    return len(pcm) / 2 / rate

NOXING = ["-write_xing", "0"]

# ① 采样率 × 声道 × 码率矩阵：答出来的必须与解码结果逐采样一致
random.seed(7)
worst, answered, refused = 0.0, 0, []
for rate in (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000):
    for ch in (1, 2):
        for kbps in (32, 64, 128, 192, 320):
            if rate < 16000 and kbps > 64: continue          # 低采样率下编码器不收这些码率
            if rate in (16000, 22050, 24000) and kbps > 160: continue
            p = synth(f"cbr_{rate}_{ch}_{kbps}", round(random.uniform(0.4, 9), 3), rate, ch,
                      ["-b:a", f"{kbps}k", *NOXING])
            mine = mp3_duration(p)
            if mine is None:
                refused.append(os.path.basename(p)); continue
            answered += 1
            worst = max(worst, abs(mine - decoded_seconds(p)))
check("① CBR 矩阵：全部作答", not refused, refused)
check("① CBR 矩阵：与解码采样数一致（<1µs）", answered > 40 and worst < 1e-6, f"{answered} 个, 最大偏差 {worst*1000:.4f}ms")

# ② VBR（无 Xing 头）：帧长逐帧变化，ffprobe 在这里是估算，解析器必须是精确的
for q in (0, 2, 5, 9):
    p = synth(f"vbr_q{q}", 6.3, 24000, 1, ["-q:a", str(q), *NOXING], src="anoisesrc=c=pink")
    mine = mp3_duration(p)
    check(f"② VBR q{q}（无 Xing）与解码一致", mine is not None and abs(mine - decoded_seconds(p)) < 1e-6, mine)

# ③ 标签：ID3v2.3/2.4（含大标签）、ID3v1、无标签；各自不影响结果
for label, args in (
    ("ID3v2.4 + 标题", ["-id3v2_version", "4", "-metadata", "title=hello world"]),
    ("ID3v2.3 + 3KB 注释", ["-id3v2_version", "3", "-metadata", "comment=" + "x" * 3000]),
    ("仅 ID3v1", ["-write_id3v1", "1", "-id3v2_version", "0"]),
    ("无任何标签", ["-id3v2_version", "0"]),
):
    p = synth("tag_" + label.replace(" ", "_").replace("+", ""), 3.3, 22050, 1, ["-b:a", "128k", *NOXING, *args])
    mine = mp3_duration(p)
    check(f"③ {label}", mine is not None and abs(mine - decoded_seconds(p)) < 1e-6, mine)

# ④ 带 Xing/Info 头 → 拒答（编码器延迟的裁法各解码器不一致），交给 ffprobe
for label, args, src in (("CBR + Info 头", ["-b:a", "128k"], "sine=f=300"),
                         ("VBR + Xing 头", ["-q:a", "3"], "anoisesrc=c=pink")):
    p = synth("hdr_" + label[:3], 4.321, 24000, 1, args, src=src)
    check(f"④ {label} → 拒答", mp3_duration(p) is None)

# ⑤ 受损/异常输入：拒答，绝不乱猜
good = open(synth("plain", 3.3, 22050, 1, ["-b:a", "128k", *NOXING, "-id3v2_version", "0"]), "rb").read()
def write(name, data):
    p = f"{TMP}/{name}.mp3"; open(p, "wb").write(data); return p
check("⑤ 末帧被截断 → 拒答", mp3_duration(write("trunc", good[: len(good) // 2 + 7])) is None)
check("⑤ 空文件 → 拒答", mp3_duration(write("empty", b"")) is None)
check("⑤ 随机字节 → 拒答", mp3_duration(write("junk", os.urandom(5000))) is None)
check("⑤ 只有 3 个字节 → 拒答", mp3_duration(write("tiny", b"\xff\xfb\x90")) is None)
wav = f"{TMP}/not_mp3.wav"
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", wav])
check("⑤ WAV 文件 → 拒答", mp3_duration(wav) is None)
check("⑤ 不存在的路径 → 拒答", mp3_duration(f"{TMP}/nope.mp3") is None)

mid = bytearray(good)
i = 2000
while not (good[i] == 0xFF and good[i + 1] & 0xE0 == 0xE0 and good[i + 2] != 0): i += 1
mid[i:i + 4] = b"\x00\x00\x00\x00"
check("⑤ 中途一个帧头被破坏 → 拒答", mp3_duration(write("hdr_dead", bytes(mid))) is None)
payload = bytearray(good); payload[len(payload) // 2: len(payload) // 2 + 50] = b"\x12" * 50
p = write("payload_dead", bytes(payload))
check("⑤ 帧负载被破坏（帧结构完好）→ 与解码仍一致",
      mp3_duration(p) is not None and abs(mp3_duration(p) - decoded_seconds(p)) < 1e-6)

# ⑥ 尾巴：零填充 / APEv2 可容忍；其它垃圾不行
check("⑥ 尾部 1000 个零字节", abs(mp3_duration(write("zeros", good + b"\x00" * 1000)) - decoded_seconds(f"{TMP}/zeros.mp3")) < 1e-6)
check("⑥ 尾部 APEv2 标签", mp3_duration(write("ape", good + b"APETAGEX" + b"\x00" * 200)) is not None)
check("⑥ 尾部是别的垃圾 → 拒答", mp3_duration(write("tailjunk", good + b"hello world, not audio")) is None)

# ⑦ 中途换采样率 → 拒答
a = synth("a", 1, 24000, 1, ["-b:a", "64k", *NOXING, "-id3v2_version", "0"])
b = synth("b", 1, 44100, 1, ["-b:a", "64k", *NOXING, "-id3v2_version", "0"])
check("⑦ 两段不同采样率拼接 → 拒答", mp3_duration(write("mixed", open(a, "rb").read() + open(b, "rb").read())) is None)

# ⑧ 接线：probe_duration 对 MP3 不起进程；解析器拒答时仍回落到 ffprobe；缓存按 mtime 失效
calls = []
real_run = subprocess.run
def counting_run(cmd, *a_, **k_):
    calls.append(cmd[0]); return real_run(cmd, *a_, **k_)
subtitle_service.subprocess.run = counting_run
try:
    subtitle_service._duration_cache.clear()
    p = synth("wired", 2.5, 22050, 1, ["-b:a", "128k", *NOXING])
    calls.clear()
    d = subtitle_service.probe_duration(p)
    spawned = list(calls)              # 先记下：下面的 decoded_seconds 自己也会起进程
    check("⑧ MP3：不起 ffprobe", d is not None and abs(d - decoded_seconds(p)) < 1e-6 and spawned == [], spawned)

    px = synth("wired_xing", 2.5, 22050, 1, ["-b:a", "128k"])      # 默认带 Info 头 → 解析器拒答
    calls.clear()
    dx = subtitle_service.probe_duration(px)
    check("⑧ 解析器拒答 → 回落 ffprobe 并拿到时长", dx is not None and abs(dx - 2.5) < 0.1 and calls.count("ffprobe") == 1, (dx, calls))

    calls.clear()
    subtitle_service.probe_duration(p); subtitle_service.probe_duration(px)
    check("⑧ 第二次走缓存，不再起进程", calls == [], calls)

    # TTS 会覆盖同一路径：内容变了，缓存必须随 mtime/大小失效
    synth("wired", 4.0, 22050, 1, ["-b:a", "128k", *NOXING])
    os.utime(p, ns=(os.stat(p).st_atime_ns, os.stat(p).st_mtime_ns + 5_000_000))
    d2 = subtitle_service.probe_duration(p)
    # 编码器把 4.0s 补齐到整帧，所以对照解码结果，而不是对照"4.0"
    check("⑧ 同一路径被覆盖后返回新时长（不是缓存的旧值）",
          d2 is not None and abs(d2 - decoded_seconds(p)) < 1e-6 and abs(d2 - d) > 1.0, (d, d2))

    wav_d = subtitle_service.probe_duration(wav)
    check("⑧ 非 MP3 扩展名仍走 ffprobe", wav_d is not None and abs(wav_d - 1.0) < 0.05 and "ffprobe" in calls, wav_d)
finally:
    subtitle_service.subprocess.run = real_run

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_audio_probe():
    """pytest 入口：脚本在 import 时已执行完毕。"""
    assert not fails, fails
