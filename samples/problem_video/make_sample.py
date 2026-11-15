#!/usr/bin/env python3
"""
合成一个"有问题的"测试视频，带逐事件的真值，供以后评估 VAD / ASR / omni review 用。

为什么是合成的：真实素材里的"谁在什么时候笑、哪两句话叠在一起"没有标准答案，
合成的有。它覆盖 ASR 最容易出问题的几类：

  speech          正常说话（两个说话人，macOS `say` 的中文声音）
  overlap         两人同时说话（时间上有交集）
  nonverbal_human 人声轨上的非语言：笑声 / 叹息 / 咳嗽（单独出现、紧贴句尾、压在说话中间）
  background      风声（持续、有阵风）/ 撞击声（关门）—— 在"背景轨"里
  music           纯音乐段，之后垫在说话下面

输出（都在本目录）：
  problem_sample.mp4     成片：画面是 testsrc2，音频是下面两条混在一起
  speech_stem.flac       理想的"人声轨"（说话 + 人声非语言），相当于完美分离后的 vocals
  background_stem.flac   理想的"背景轨"（风 / 撞击 / 音乐 / 底噪）
  truth.json             逐事件真值（起止秒、类别、说话人、文本、在哪条轨上）

注意：这是合成信号，不是真实录音。人声是系统 TTS，笑声/叹息/咳嗽是用共振峰滤波的噪声和
谐波合成的"够像"的替身。它适合验证流程和量化"候选检测"这类确定性逻辑，不适合判断
模型对真实笑声的识别率 —— 那需要真实片段。

依赖：macOS（`say`）、ffmpeg、numpy、scipy。固定随机种子，重跑得到同样的文件。

    python3 make_sample.py          # 生成
    python3 make_sample.py --check  # 只对已生成的文件做自检（各事件在对应轨上是否可闻）
"""
import json
import os
import subprocess
import sys
import tempfile
import wave

import numpy as np
from scipy import signal

SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
VOICES = {"A": "Flo (中文（中国大陆）)", "B": "Reed (中文（中国大陆）)"}
FILES = {
    "video": "problem_sample.mp4",
    "speech_stem": "speech_stem.flac",
    "background_stem": "background_stem.flac",
    "truth": "truth.json",
}
rng = np.random.default_rng(20261115)


# ----------------------------------------------------------------------------- 信号工具

def bp(x, lo, hi, order=2):
    sos = signal.butter(order, [lo, hi], btype="band", fs=SR, output="sos")
    return signal.sosfilt(sos, x)


def lp(x, fc, order=4):
    return signal.sosfilt(signal.butter(order, fc, btype="low", fs=SR, output="sos"), x)


def formants(x, spec):
    """(中心频率, 带宽, 增益) 的带通叠加：粗糙但够用的共振峰滤波。"""
    out = np.zeros_like(x)
    for f, bw, g in spec:
        out += g * bp(x, max(20.0, f - bw / 2), min(SR / 2 - 100, f + bw / 2))
    return out


def peak_to(x, db):
    return x / (np.max(np.abs(x)) + 1e-12) * 10 ** (db / 20)


def rms_to(x, db):
    return x / (np.sqrt(np.mean(x ** 2)) + 1e-12) * 10 ** (db / 20)


def trim(x, rel=0.02):
    idx = np.where(np.abs(x) > rel * np.max(np.abs(x)))[0]
    return x[idx[0]: idx[-1] + 1]


# ----------------------------------------------------------------------------- 声音合成

def tts(voice, text):
    with tempfile.TemporaryDirectory() as d:
        aiff = os.path.join(d, "x.aiff")
        try:
            subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"`say -v {voice!r}` 失败：{e.stderr.decode(errors='ignore')[:200]}\n"
                             "用 `say -v '?' | grep zh_` 看本机有哪些中文声音，改 VOICES。")
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", aiff, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                           capture_output=True, check=True)
    return trim(np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64))


def synth_laugh(n_ha=6, f0=250.0):
    out = np.zeros(int(SR * (n_ha * 0.25 + 0.3)))
    onset = 0.0
    for k in range(n_ha):
        dur = 0.12 + 0.01 * rng.random()
        L = int(dur * SR)
        tt = np.arange(L) / SR
        f = f0 * (1 - 0.18 * k / n_ha) * (1 - 0.15 * tt / dur) * (1 + 0.02 * np.sin(2 * np.pi * 6 * tt))
        phase = 2 * np.pi * np.cumsum(f) / SR
        src = sum(np.sin(h * phase) / h for h in range(1, 15)) + 0.35 * rng.standard_normal(L)
        env = np.minimum(1, tt / 0.012) * np.exp(-tt / 0.07)
        seg = formants(src, [(800, 250, 1.0), (1250, 300, 0.7), (2700, 500, 0.3)]) * env * (0.82 ** k)
        s = int(onset * SR)
        out[s:s + L] += seg
        onset += 0.19 + 0.015 * k
    return trim(out)


def synth_sigh(dur=1.1):
    n = int(dur * SR)
    t = np.arange(n) / SR
    breath = rms_to(bp(rng.standard_normal(n), 700, 6000), 0)
    env = np.where(t < 0.25, np.sin(0.5 * np.pi * t / 0.25) ** 2, np.exp(-(t - 0.25) / 0.35))
    f = 200 - 60 * t / dur
    ph = 2 * np.pi * np.cumsum(f) / SR
    voiced = rms_to(formants(sum(np.sin(h * ph) / h for h in range(1, 12)), [(700, 250, 1), (1150, 300, 0.6)]), 0)
    return trim((0.8 * breath + 0.35 * voiced) * env)


def synth_cough():
    out = np.zeros(int(0.5 * SR))
    for onset, d in ((0.0, 0.10), (0.23, 0.08)):
        L = int((d + 0.05) * SR)
        tt = np.arange(L) / SR
        burst = formants(rng.standard_normal(L), [(650, 300, 1), (1500, 500, 0.8), (3000, 800, 0.4)])
        burst = burst / np.max(np.abs(burst)) * np.minimum(1, tt / 0.004) * np.exp(-tt / 0.035)
        thump = np.sin(2 * np.pi * 130 * tt) * np.exp(-tt / 0.05) * 0.5
        s = int(onset * SR)
        out[s:s + L] += burst + thump
    return trim(out)


def synth_impact():
    n = int(1.0 * SR)
    t = np.arange(n) / SR
    thump = np.sin(2 * np.pi * 68 * t) * np.exp(-t / 0.14) + 0.6 * np.sin(2 * np.pi * 42 * t) * np.exp(-t / 0.2)
    click = lp(rng.standard_normal(n), 3500) * np.exp(-t / 0.012) * 1.5
    tail = lp(rng.standard_normal(n), 700) * np.exp(-t / 0.28) * 0.35
    x = thump + click + tail
    ramp = int(0.002 * SR)
    x[:ramp] *= np.linspace(0, 1, ramp)
    return trim(x, 0.01)


def synth_wind(dur):
    n = int(dur * SR)
    t = np.arange(n) / SR
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = 1.0
    x = lp(np.fft.irfft(spec / np.sqrt(f), n), 1100, 2)
    gust = np.clip(1 + 0.55 * np.sin(2 * np.pi * 0.21 * t + 0.7) + 0.3 * np.sin(2 * np.pi * 0.067 * t + 2.1), 0.15, None)
    fade = np.minimum(1, np.minimum(t, dur - t) / 1.2)
    return x / np.std(x) * gust * fade


def synth_music(dur):
    chords = [(220.0, 261.63, 329.63), (174.61, 220.0, 261.63), (261.63, 329.63, 392.0), (196.0, 246.94, 293.66)]
    n = int(dur * SR)
    out = np.zeros(n)
    step = 0.4
    for i in range(int(dur / step) + 1):
        f = chords[(i // 4) % 4][i % 3] * 2
        L = int(0.9 * SR)
        tt = np.arange(L) / SR
        note = (np.sin(2 * np.pi * f * tt) + 0.35 * np.sin(2 * np.pi * 2 * f * tt) + 0.12 * np.sin(2 * np.pi * 3 * f * tt))
        note *= np.exp(-tt / 0.35) * np.minimum(1, tt / 0.005)
        s = int(i * step * SR)
        e = min(n, s + L)
        out[s:e] += note[: e - s]
    t = np.arange(n) / SR
    for fr in chords[0]:
        out += 0.15 * np.sin(2 * np.pi * fr * t)
    fade = np.minimum(1, np.minimum(t, dur - t) / 0.4)
    return out * fade


# ----------------------------------------------------------------------------- 布局

def build():
    placed = []     # (轨, 起点秒, 采样)
    truth = []

    def put(track, t, x, **meta):
        placed.append((track, t, x))
        ev = {"start": round(t, 3), "end": round(t + len(x) / SR, 3), "track": track, **meta}
        truth.append(ev)
        return ev

    def speak(who, text, t, **meta):
        return put("speech_stem", t, peak_to(tts(VOICES[who], text), -6), category="speech", speaker=who, text=text, **meta)

    t = 1.0
    a1 = speak("A", "大家好，欢迎收看今天的节目。", t); t = a1["end"] + 0.5
    b1 = speak("B", "今天我们聊一聊视频翻译这件事。", t); t = b1["end"] + 0.3

    wind_start = b1["start"] + 0.6
    t += 0.4
    imp = put("background_stem", t, peak_to(synth_impact(), -4), category="background", label="impact",
              note="关门的撞击声，独立出现，ASR 可能漏掉，也可能误识成一个字")
    t = imp["end"] + 0.7
    a2 = speak("A", "你听到刚才那声巨响了吗？", t); t = a2["end"] + 0.4
    lg1 = put("speech_stem", t, peak_to(synth_laugh(6, 250), -8), category="nonverbal_human", label="laugh", speaker="B",
              note="独立的笑声：人声轨上有声音，ASR 通常没有句子"); t = lg1["end"] + 0.6
    sg = put("speech_stem", t, peak_to(synth_sigh(), -16), category="nonverbal_human", label="sigh", speaker="A",
             note="很轻的叹息"); t = sg["end"] + 0.5
    a3 = speak("A", "好吧，我们继续。", t); t = a3["end"] + 0.4
    wind_end = a3["end"] + 0.3

    b2 = speak("B", "接下来是第二个问题，关于字幕的位置。", t)
    cough_t = b2["start"] + 0.55 * (b2["end"] - b2["start"])
    cg = put("speech_stem", cough_t, peak_to(synth_cough(), -9), category="nonverbal_human", label="cough", speaker="A",
             overlaps=[b2["text"]], note="咳嗽压在别人说话中间：无法与说话分开，只能标注")
    t = max(b2["end"], cg["end"]) + 0.5

    a4 = speak("A", "我觉得这个方案很好，应该马上执行。", t, overlap_group=1)
    b3 = speak("B", "不对，我完全不同意，成本太高了。", t + 0.3, overlap_group=1)
    ov_start, ov_end = b3["start"], min(a4["end"], b3["end"])
    truth.append({"start": round(ov_start, 3), "end": round(ov_end, 3), "track": "speech_stem", "category": "overlap",
                  "speakers": ["A", "B"], "note": "两人同时说话；ASR 的单值 speaker 无法表达"})
    t = max(a4["end"], b3["end"]) + 0.6

    music_start = t
    t += 2.5
    a5 = speak("A", "好，回到正题。我们来看最后一个例子。", t); t = a5["end"] + 0.4
    b4 = speak("B", "谢谢大家，我们下期再见。", t)
    lg2 = put("speech_stem", b4["end"] + 0.12, peak_to(synth_laugh(5, 270), -8), category="nonverbal_human", label="laugh",
              speaker="A", note="紧贴句尾的笑声：ASR 可能把它并进上一句")
    music_end = lg2["end"] + 0.4
    total = lg2["end"] + 1.0

    put("background_stem", wind_start, rms_to(synth_wind(wind_end - wind_start), -30), category="background", label="wind",
        note="持续的风声，带阵风，垫在好几句话下面")
    put("background_stem", music_start, rms_to(synth_music(music_end - music_start), -26), category="music", label="music",
        note=f"{music_start:.1f}–{a5['start']:.1f} 秒是纯音乐，之后垫在说话下面直到结尾")
    return placed, truth, total


def render(placed, total):
    n = int((total + 0.2) * SR)
    stems = {"speech_stem": np.zeros(n), "background_stem": np.zeros(n)}
    for track, t, x in placed:
        s = int(t * SR)
        stems[track][s:s + len(x)] += x[: n - s]
    # 底噪：一点点，免得"没有事件的地方"是数字静音（真实录音里不会有）
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / SR)
    f[0] = 1.0
    stems["background_stem"] += rms_to(np.fft.irfft(spec / np.sqrt(f), n), -55)
    mix = stems["speech_stem"] + stems["background_stem"]
    gain = 10 ** (-1 / 20) / np.max(np.abs(mix))      # 三条用同一个增益，相对电平不变
    return {k: v * gain for k, v in stems.items()}, mix * gain


def write_wav(path, x):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def generate():
    placed, truth, total = build()
    stems, mix = render(placed, total)
    duration = len(mix) / SR
    with tempfile.TemporaryDirectory() as d:
        for name, x in (("mix", mix), ("speech", stems["speech_stem"]), ("bg", stems["background_stem"])):
            write_wav(os.path.join(d, f"{name}.wav"), x)
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24:duration={duration:.3f}",
               "-i", os.path.join(d, "mix.wav"), "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-shortest",
               os.path.join(HERE, FILES["video"]))
        ffmpeg("-i", os.path.join(d, "speech.wav"), "-c:a", "flac", os.path.join(HERE, FILES["speech_stem"]))
        ffmpeg("-i", os.path.join(d, "bg.wav"), "-c:a", "flac", os.path.join(HERE, FILES["background_stem"]))

    truth.sort(key=lambda e: (e["start"], e["end"]))
    for i, e in enumerate(truth, 1):
        e["id"] = f"ev{i:02d}"
    speech_lines = [e for e in truth if e["category"] == "speech"]

    def intersects(a, b):
        return min(a["end"], b["end"]) - max(a["start"], b["start"]) > 0.05

    truth_doc = {
        "about": "合成测试视频的逐事件真值。时间单位秒，对应 problem_sample.mp4 的时间轴。",
        "synthetic": True,
        "sample_rate": SR,
        "duration": round(duration, 3),
        "files": FILES,
        "voices": VOICES,
        "events": truth,
        # 派生出来的"应该被发现"的清单，写死在这里是为了以后测试不必重新推导：
        "expected": {
            "speech_lines": [e["id"] for e in speech_lines],
            "uncovered_voice": [e["id"] for e in truth if e["category"] == "nonverbal_human"
                                and not any(intersects(e, s) for s in speech_lines)],
            "voice_inside_speech": [e["id"] for e in truth if e["category"] == "nonverbal_human"
                                    and any(intersects(e, s) for s in speech_lines)],
            "overlap": [e["id"] for e in truth if e["category"] == "overlap"],
            "background_only": [e["id"] for e in truth if e["category"] in ("background", "music")],
        },
    }
    with open(os.path.join(HERE, FILES["truth"]), "w", encoding="utf-8") as f:
        json.dump(truth_doc, f, ensure_ascii=False, indent=2)
    print(f"生成完毕：{duration:.1f} 秒，{len(truth)} 个事件 → {HERE}")


# ----------------------------------------------------------------------------- 自检

def load(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.float32).astype(np.float64)


def dbfs(x):
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12) if len(x) else float("-inf")


def check():
    doc = json.load(open(os.path.join(HERE, FILES["truth"]), encoding="utf-8"))
    stems = {"speech_stem": load(os.path.join(HERE, FILES["speech_stem"])),
             "background_stem": load(os.path.join(HERE, FILES["background_stem"]))}
    mix = load(os.path.join(HERE, FILES["video"]))
    print(f"视频音轨 {len(mix) / SR:.2f}s（真值记录 {doc['duration']}s）\n")
    print(f"{'id':<5}{'类别':<17}{'起':>7}{'止':>7}  {'事件轨内':>8}{'同窗另一轨':>10}  说明")
    bad = 0
    for e in doc["events"]:
        s, t = int(e["start"] * SR), int(e["end"] * SR)
        own = e["track"]
        other = "background_stem" if own == "speech_stem" else "speech_stem"
        d_own, d_other = dbfs(stems[own][s:t]), dbfs(stems[other][s:t])
        # 事件在自己的轨上必须明显高于底噪；纯音乐/风声在另一轨上不该有人声
        floor = -50
        ok = d_own > floor
        bad += not ok
        label = e.get("label") or e.get("text", "")[:10]
        print(f"{e['id']:<5}{e['category']:<17}{e['start']:>7.2f}{e['end']:>7.2f}  {d_own:>7.1f}dB{d_other:>9.1f}dB  "
              f"{label} {'' if ok else '  <-- 太轻'}")
    silent = np.ones(len(mix), bool)
    for e in doc["events"]:
        silent[int(e["start"] * SR): int(e["end"] * SR)] = False
    print(f"\n无事件区间的混音电平：{dbfs(mix[silent[:len(mix)]]):.1f} dBFS（应远低于事件）")
    print("自检", "通过" if not bad else f"有 {bad} 个事件过轻")
    return bad


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(1 if check() else 0)
    generate()
    sys.exit(1 if check() else 0)
