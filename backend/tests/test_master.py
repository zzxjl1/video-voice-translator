"""
母带（响度归一 + 限幅）：决定逻辑单测 + 用真实 ffmpeg 渲染后**重新测量成片**。

最后一类才是重点：断言的是成片的整合响度与真峰，不是"命令行拼对了"。需要 ffmpeg。
"""
import os, re, subprocess, sys, tempfile, wave
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
import numpy as np
from app.services import export_service as ex, mix_service as M

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

MASTER = {"targetLufs": M.TARGET_LUFS, "peakLimitDbfs": M.PEAK_LIMIT_DBFS,
          "maxLimitingDb": M.MAX_LIMITING_DB, "maxBoostDb": M.MAX_BOOST_DB}

# ---------------------------------------------------------------- ① master_gain（纯函数）
g = M.master_gain(-20.0, -6.0, MASTER)
check("① 安静混音 → 补到目标", g["gainDb"] == 4.0 and g["limitedBy"] == "none" and g["expectedLimitDb"] == 0.0, g)

g = M.master_gain(-12.0, -2.0, MASTER)
check("① 偏响混音 → 往下拉", g["gainDb"] == -4.0 and g["limitedBy"] == "none", g)

# 想要 +10 dB，但峰值只剩 4.5 dB 头部 + 3 dB 限幅预算 = 7.5 → 被峰值护栏夹住
g = M.master_gain(-26.0, -6.0, MASTER)
check("① 峰值太尖 → 少加，不硬压", g["gainDb"] == 7.5 and g["limitedBy"] == "peak" and g["wantedDb"] == 10.0, g)
check("① ……限幅器预计压量恰为预算", g["expectedLimitDb"] == M.MAX_LIMITING_DB, g)

# 峰值已经超上限很多：护栏可以是负数 —— 整体往下拉，而不是硬限
g = M.master_gain(-14.0, 6.0, MASTER)
check("① 峰值远超上限 → 增益为负", g["gainDb"] < 0 and g["limitedBy"] == "peak", g)

g = M.master_gain(-45.0, -44.0, MASTER)
check("① 抬升有上限", g["gainDb"] == M.MAX_BOOST_DB and g["limitedBy"] == "boost", g)

for label, lufs in (("纯静音（-70）", -70.0), ("测不出（None）", None), ("NaN", float("nan"))):
    g = M.master_gain(lufs, float("-inf"), MASTER)
    check(f"① {label} → 增益 0，只留限幅", g["gainDb"] == 0.0 and g["limitedBy"] == "unmeasurable", g)

# ---------------------------------------------------------------- ② 解析 ebur128 输出
REAL = """[Parsed_ebur128_0 @ 0xb5a814900] Summary:

  Integrated loudness:
    I:         -21.7 LUFS
    Threshold: -31.7 LUFS

  Loudness range:
    LRA:        20.0 LU
    Threshold: -41.7 LUFS

  Sample peak:
    Peak:      -18.1 dBFS
"""
check("② 解析真实 Summary", ex.parse_ebur128_summary(REAL) == (-21.7, -18.1), ex.parse_ebur128_summary(REAL))
check("② 静音（-70 / -inf 峰值）→ 响度 None",
      ex.parse_ebur128_summary(REAL.replace("-21.7", "-70.0").replace("-18.1", "-inf")) == (None, float("-inf")))
check("② 正的峰值也能读", ex.parse_ebur128_summary(REAL.replace("-18.1", "0.8"))[1] == 0.8)
for label, text in (("空输出", ""), ("缺峰值", REAL.split("  Sample peak:")[0]), ("乱码", "garbage")):
    try:
        ex.parse_ebur128_summary(text); ok = False
    except RuntimeError:
        ok = True
    check(f"② {label} → 抛错而不是猜", ok)

# 多次出现 Summary（长输出里滤镜图不止一次打印）取最后一个
twice = REAL.replace("-21.7 LUFS\n    Threshold", "-99.0 LUFS\n    Threshold", 1) + "\n" + REAL
check("② 取最后一段 Summary", ex.parse_ebur128_summary(twice)[0] == -21.7)

# ---------------------------------------------------------------- ③ 滤镜串
f = ex.master_filter("t", -20.0, -6.0, MASTER)
check("③ 滤镜串：先增益，再升采样，最后限幅（顺序是有意的）",
      re.fullmatch(r"volume=4\.000dB,aresample=48000,alimiter=limit=0\.841\d+:attack=5:release=50:level=disabled:latency=1", f) is not None, f)
f = ex.master_filter("t", None, float("-inf"), {**MASTER, "peakLimitDbfs": -40.0})
check("③ 限幅线性值被夹在 -24 dB（0.0631，在 alimiter 的合法范围 [0.0625, 1] 内）", "limit=0.063096:" in f, f)
f = ex.master_filter("t", None, float("-inf"), {**MASTER, "peakLimitDbfs": 3.0})
check("③ ……上限为正数也夹到 0 dB（limit=1）", "limit=1.000000:" in f, f)

# ---------------------------------------------------------------- ④ 渲染后重新测量成片
TMP = tempfile.mkdtemp()
SR = 24000
DUR = 8.0

def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-400:]
    return r

def write_wav(name, samples):
    path = f"{TMP}/{name}.wav"
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes(np.clip(np.round(samples * 32767), -32768, 32767).astype(np.int16).tobytes())
    return path

t = np.arange(int(DUR * SR)) / SR
rng = np.random.default_rng(3)
def speech_like(amp):                       # 在 0.25 s 的节奏上起伏的宽带信号，不是单音
    env = 0.5 + 0.5 * np.sin(2 * np.pi * 4 * t)
    return amp * env * (np.sin(2 * np.pi * 220 * t) + 0.4 * rng.standard_normal(len(t)) * 0.3)

VIDEO = f"{TMP}/v.mp4"
run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=160x120:d={DUR}",
     "-f", "lavfi", "-i", f"sine=f=200:d={DUR}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
     "-c:a", "aac", VIDEO])

def render(label, timeline, bed, master=MASTER):
    tl = write_wav(label + "_tl", timeline)
    bd = write_wav(label + "_bed", bed) if bed is not None else None
    out = f"{TMP}/{label}.mp4"
    ex._mux("t", VIDEO, tl, bd, out, DUR, master=master)
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-v", "info", "-i", out, "-map", "0:a:0", "-filter_complex",
             "[0:a]ebur128=peak=true:framelog=quiet[o]", "-map", "[o]", "-f", "null", "-"])
    tail = r.stderr[r.stderr.rfind("Summary:"):]
    lufs = float(re.search(r"I:\s+(-?(?:inf|[\d.]+)) LUFS", tail).group(1))
    tp = float(re.search(r"True peak:\s+Peak:\s+(-?(?:inf|[\d.]+)) dBFS", tail).group(1))
    vcodec = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                  "-of", "default=nw=1:nk=1", out]).stdout.strip()
    return lufs, tp, vcodec, out

TARGET, CEIL = MASTER["targetLufs"], MASTER["peakLimitDbfs"]

# 安静：-30 LUFS 量级的配音 + 很轻的垫底 → 应被抬到目标，真峰在上限内
lufs, tp, vcodec, _ = render("quiet", speech_like(0.04), speech_like(0.005))
check("④ 安静混音被抬到目标响度（±0.7 LU）", abs(lufs - TARGET) < 0.7, f"{lufs:.2f} LUFS")
check("④ ……真峰不超上限", tp <= CEIL + 0.05, f"{tp:.2f} dBTP")
check("④ 视频流直拷（没被重编码）", vcodec == "h264", vcodec)

# 偏响：采样峰值冲过 0 dBFS 的混音 → 响度归到目标，真峰被限在上限内
lufs, tp, _, _ = render("hot", np.clip(speech_like(1.6), -3.0, 3.0), speech_like(0.2))
check("④ 偏响混音拉回目标响度（±0.7 LU）", abs(lufs - TARGET) < 0.7, f"{lufs:.2f} LUFS")
check("④ ……冲过 0 dBFS 的峰值被限在上限内", tp <= CEIL + 0.05, f"{tp:.2f} dBTP")

# 峰值太尖：很轻的底噪 + 零星满幅脉冲 → 想补的增益被峰值护栏夹住，停在目标之下，但真峰仍在上限内
spiky = 0.01 * rng.standard_normal(len(t))
spiky[:: SR] = 0.99                          # 每秒一个满幅脉冲
lufs, tp, _, _ = render("spiky", spiky, np.zeros(len(t)))
check("④ 尖峰混音：停在目标之下而不是硬压到目标", lufs < TARGET - 3.0, f"{lufs:.2f} LUFS")
check("④ ……真峰仍在上限内", tp <= CEIL + 0.05, f"{tp:.2f} dBTP")

# 没有垫底（bed_path=None）也要能出片
lufs, tp, _, out = render("nobed", speech_like(0.05), None)
check("④ 无垫底：成片响度到目标", abs(lufs - TARGET) < 0.7 and tp <= CEIL + 0.05, f"{lufs:.2f} LUFS / {tp:.2f} dBTP")

# 纯静音：不能崩，也不能把底噪抬上去
lufs, tp, _, out = render("silent", np.zeros(len(t)), np.zeros(len(t)))
check("④ 纯静音：能出片且仍是静音（不凭空抬增益）", os.path.exists(out) and tp < -60, f"{tp} dBTP")

# 配置里把目标改了，成片跟着改（策略数值不是写死在滤镜里的）
lufs, _, _, _ = render("t23", speech_like(0.04), speech_like(0.005), {**MASTER, "targetLufs": -23.0})
check("④ 目标改成 -23 → 成片到 -23（±0.7 LU）", abs(lufs + 23.0) < 0.7, f"{lufs:.2f} LUFS")

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_master():
    """pytest 入口：脚本在 import 时已执行完毕。"""
    assert not fails, fails
