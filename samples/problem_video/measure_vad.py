#!/usr/bin/env python3
"""
在合成样本上量 Silero VAD（ONNX）和纯能量检测：各自能发现什么、误报什么。

对比基准：
  · truth.json 的逐事件真值
  · asr_baseline/ 里真实 ASR 的输出（run_asr.py 跑出来的），用来算"候选" =
    有声音、但 ASR 句子没覆盖到的区间

两条轨：
  vocals  理想的人声轨（相当于人声分离之后）
  mix     没做分离时流水线实际送去的混音

用法（需要装了 onnxruntime 的 Python；Silero 的 .onnx 路径从命令行给）：
    /tmp/silero_env/bin/python measure_vad.py /path/to/silero_vad.onnx
"""
import json
import os
import subprocess
import sys
import time

import numpy as np
import onnxruntime as ort

HERE = os.path.dirname(os.path.abspath(__file__))
SR = 16000
WIN, CTX = 512, 64          # Silero v5/v6 在 16 kHz 下：每帧 512 采样（32 ms），前面带 64 采样上下文
GRID = 0.01                 # 统一的评估网格：10 ms


def load(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.float32).copy()


# ----------------------------------------------------------------------------- 两个检测器

def silero_probs(sess, x):
    state = np.zeros((2, 1, 128), dtype=np.float32)
    ctx = np.zeros((1, CTX), dtype=np.float32)
    sr = np.array(SR, dtype=np.int64)
    n = len(x) // WIN * WIN
    out = []
    for i in range(0, n, WIN):
        chunk = np.concatenate([ctx, x[i:i + WIN][None, :]], axis=1)
        p, state = sess.run(None, {"input": chunk, "state": state, "sr": sr})
        ctx = chunk[:, -CTX:]
        out.append(float(p[0, 0]))
    return np.array(out)                           # 每 32 ms 一个概率


def energy_active(x, margin_db=12.0, win=0.04, hop=0.02):
    w, h = int(win * SR), int(hop * SR)
    db = np.array([20 * np.log10(np.sqrt(np.mean(x[i:i + w].astype(np.float64) ** 2)) + 1e-9)
                   for i in range(0, len(x) - w, h)])
    return db > np.percentile(db, 10) + margin_db, hop


def to_grid(flags, step, total):
    """把"每 step 秒一个布尔"重采样到 10 ms 网格。"""
    n = int(total / GRID)
    t = np.arange(n) * GRID
    idx = np.minimum((t / step).astype(int), len(flags) - 1)
    return np.asarray(flags)[idx].astype(bool)


def clean(mask, min_on=0.10, max_gap=0.15):
    """去掉太短的段、补上太短的缝 —— 两个检测器用同一套后处理，比较才公平。"""
    runs, s = [], None
    for i, v in enumerate(mask):
        if v and s is None:
            s = i
        if not v and s is not None:
            runs.append([s * GRID, i * GRID]); s = None
    if s is not None:
        runs.append([s * GRID, len(mask) * GRID])
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < max_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= min_on]


# ----------------------------------------------------------------------------- 评估

def overlap(a, b):
    return max(0.0, min(a[1], b[1]) - max(a[0], b[0]))


def coverage(runs, ev):
    return sum(overlap(r, (ev["start"], ev["end"])) for r in runs) / (ev["end"] - ev["start"])


def candidates(runs, asr_lines, min_uncovered=0.3):
    out = []
    for s, e in runs:
        covered = sum(overlap((s, e), (l["start_time"], l["end_time"])) for l in asr_lines)
        if (e - s) - covered > min_uncovered:
            out.append((round(s, 2), round(e, 2), round(1 - covered / (e - s), 2)))
    return out


def report(name, runs, truth, asr_lines):
    print(f"\n  ── {name}: {len(runs)} 段 ──")
    ev_by_cat = {}
    for e in truth["events"]:
        ev_by_cat.setdefault(e["category"], []).append(e)
    for cat, label in (("nonverbal_human", "人声非语言"), ("background", "背景(风/撞击)"), ("music", "音乐")):
        for e in ev_by_cat.get(cat, []):
            c = coverage(runs, e)
            print(f"     {e['id']} {label:<10}{e.get('label',''):<8}{e['start']:6.2f}-{e['end']:6.2f}   被检测为'有声'的比例 {c:4.0%}")
    sp = [e for e in ev_by_cat["speech"]]
    miss = [e for e in sp if coverage(runs, e) < 0.5]
    print(f"     台词 {len(sp)} 句，被覆盖不到一半的：{[e['id'] for e in miss] or '无'}")
    # 误报：落在任何真值事件之外的"有声"时长
    spans = [(e["start"], e["end"]) for e in truth["events"] if e["category"] != "overlap"]
    fp = sum(max(0.0, (r[1] - r[0]) - sum(overlap(r, s) for s in spans)) for r in runs)
    print(f"     事件之外被判为'有声'的总时长（误报）：{fp:.2f} s")
    cands = candidates(runs, asr_lines)
    print(f"     相对真实 ASR 的候选（有声但没被台词覆盖 >0.3s）：{len(cands)} 个")
    for s, e, f in cands:
        hits = [x["id"] + ":" + (x.get("label") or x["category"]) for x in truth["events"]
                if x["category"] != "overlap" and overlap((s, e), (x["start"], x["end"])) > 0.05
                and x["category"] != "speech"]
        print(f"        {s:6.2f}-{e:6.2f}  无ASR覆盖 {f:4.0%}   对应真值: {hits or '— 无（误报）'}")
    return cands


def main():
    model = sys.argv[1] if len(sys.argv) > 1 else None
    if not model or not os.path.exists(model):
        raise SystemExit("用法: measure_vad.py /path/to/silero_vad.onnx")
    truth = json.load(open(os.path.join(HERE, "truth.json"), encoding="utf-8"))
    sess = ort.InferenceSession(model, providers=["CPUExecutionProvider"])
    tracks = {"vocals": load(os.path.join(HERE, "speech_stem.flac")),
              "mix": load(os.path.join(HERE, "problem_sample.mp4"))}
    for name, x in tracks.items():
        total = len(x) / SR
        asr = json.load(open(os.path.join(HERE, "asr_baseline", f"asr_{name}.json"), encoding="utf-8"))["parsed_lines"]
        print(f"\n{'=' * 78}\n轨：{name}（{total:.1f} s）　真实 ASR 输出 {len(asr)} 句")
        t0 = time.perf_counter()
        probs = silero_probs(sess, x)
        dt = time.perf_counter() - t0
        print(f"  Silero 推理耗时 {dt:.2f}s（音频 {total:.1f}s → 实时率 {total / dt:.0f}x，CPU 单线程）")
        for th in (0.5, 0.3):
            runs = clean(to_grid(probs > th, WIN / SR, total))
            report(f"Silero 阈值 {th}", runs, truth, asr)
        flags, hop = energy_active(x)
        report("纯能量（底噪+12dB）", clean(to_grid(flags, hop, total)), truth, asr)


if __name__ == "__main__":
    main()
