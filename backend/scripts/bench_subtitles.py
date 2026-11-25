#!/usr/bin/env python3
"""
字幕模块性能 bench。用真实语料（~/Downloads 的 3 集专业字幕，~3000 条）。

回答三个问题：
  1. 一次导出（一集 984 条）的 build_cues + to_ass 要多久？预览拖滑块时呢？
  2. 冷启动：jieba 初始化 + 首次断行
  3. 各语言的断行耗时是否均衡（有没有某种语言慢 10 倍）

跑：cd backend && ./.venv/bin/python scripts/bench_subtitles.py
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Segment  # noqa: E402
from app.services import subtitle_service as SS  # noqa: E402
from app.services.subtitle_wrap import _best_breaks, wrap_text  # noqa: E402

DL = Path.home() / "Downloads"
FILES = sorted(DL.glob("School.Spirits*.srt"))


def parse_srt(path):
    t = path.read_text(encoding="utf-8-sig", errors="replace")
    out = []
    for b in re.split(r"\n\s*\n", t):
        if "-->" not in b:
            continue
        ls = b.split("\n")
        i = next(k for k, l in enumerate(ls) if "-->" in l)
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", ls[i])
        if not m:
            continue
        g = list(map(int, m.groups()))
        body = [l.strip() for l in ls[i + 1:] if l.strip()]
        out.append((g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000,
                    g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000, " ".join(body)))
    return out


def bench(fn, n=5):
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    ts.sort()
    return ts[len(ts) // 2]   # 中位数，去掉首次冷启动的抖动


if not FILES:
    print("no corpus")
    sys.exit(2)

all_cues = [c for f in FILES for c in parse_srt(f)]
ep = parse_srt(FILES[0])
print(f"语料：{len(all_cues)} 条（一集 {len(ep)} 条）\n")

# ── 1. 冷启动
print("━ 1. 冷启动")
t0 = time.perf_counter()
import jieba  # noqa: F401,E402
jieba.setLogLevel(60)
jieba.initialize()
t_jieba = time.perf_counter() - t0
first = [c for c in all_cues if len(c[2]) > 30][:1][0][2]
t0 = time.perf_counter()
wrap_text(first, 18)
t_first = time.perf_counter() - t0
print(f"  jieba 初始化         {t_jieba*1000:7.0f} ms   （41MB 词典，进程只做一次）")
print(f"  首次断行（含 warm）  {t_first*1000:7.1f} ms")
print(f"  → 预览第一次拖滑块会多等 ≈{(t_jieba+t_first)*1000:.0f} ms；之后不再有")

# ── 2. 纯断行：每条台词
print("\n━ 2. 纯断行（wrap_text），按长度分桶")
import jieba.posseg as pseg  # noqa: E402
for label, lo, hi in (("短 ≤15 字", 0, 15), ("中 16–30", 16, 30), ("长 31–60", 31, 60), ("超长 >60", 61, 9999)):
    texts = [c[2] for c in all_cues if lo <= len(c[2]) <= hi]
    if not texts:
        continue
    def run(texts=texts):
        for t in texts:
            wrap_text(t, 18)
    ms = bench(run, 3) * 1000
    print(f"  {label:12} {len(texts):5d} 条   {ms:7.1f} ms 总   {ms/len(texts)*1000:6.1f} µs/条")

# ── 3. 全流程：一集
print("\n━ 3. build_cues 全流程（一集 984 条）")
def make_segs(cues, bilingual=False):
    segs = []
    for i, (s, e, t) in enumerate(cues):
        seg = Segment(f"s{i}", "0", s, e, t, translated_text=t)
        segs.append(seg)
    return segs
segs_ep = make_segs(ep)
sty = SS.default_style()
sty_bi = SS.SubtitleStyle(track="bilingual")
for label, st in (("单轨", sty), ("双语", sty_bi)):
    ms = bench(lambda st=st: SS.build_cues(segs_ep, st, probe_audio=False)) * 1000
    print(f"  build_cues  {label}   {ms:7.1f} ms")
cues_ep = SS.build_cues(segs_ep, sty, probe_audio=False)
ms = bench(lambda: SS.to_ass(cues_ep, sty, 1920, 1080)) * 1000
print(f"  to_ass      单轨   {ms:7.1f} ms")
ms = bench(lambda: SS.to_srt(cues_ep, sty)) * 1000
print(f"  to_srt      单轨   {ms:7.1f} ms")
total = bench(lambda: SS.to_ass(SS.build_cues(segs_ep, sty, probe_audio=False), sty, 1920, 1080)) * 1000
print(f"  → 一次完整导出 ≈ {total:.0f} ms")

# ── 4. 预览场景：拖滑块（每次改动重新 build_cues）
print("\n━ 4. 预览拖滑块（每次 style 变化重新 build_cues）")
ms = bench(lambda: SS.build_cues(segs_ep, sty, probe_audio=False)) * 1000
print(f"  984 条：{ms:.0f} ms  → 防抖 180ms 后每秒最多 ~{1000/(ms+180):.0f} 次刷新")
# 30 分钟上限视频 ≈ 16 句/分钟 × 30 = 480 句；这里 984 条已经超过上限
print(f"  （30 分钟视频上限 ≈ 480 句，比这少一半）")

# ── 5. 预览真实场景：同一工程反复刷（lru_cache 只对到 DP 的长句有效——
#    专业字幕每行 ≤18 格，99% 的文本在 wrap_text 早退，DP 只对长句跑）
print("\n━ 5. 预览反复刷新（同一集 984 条，连续 5 次 build_cues）")
def run_preview():
    SS.build_cues(segs_ep, sty, probe_audio=False)
run_preview()   # warm：jieba 已初始化、DP 缓存已热
times = []
for _ in range(5):
    t0 = time.perf_counter()
    run_preview()
    times.append((time.perf_counter() - t0) * 1000)
print(f"  5 次：{' '.join(f'{t:.0f}' for t in times)} ms   （首次含 DP 缓存填充）")
print(f"  lru_cache 信息：{SS.wrap_text.__globals__['_best_breaks'].cache_info()}")

# ── 6. 多语言均衡
print("\n━ 6. 多语言断行耗时（同一句长度的 6 种语言，各 200 次）")
samples = {
    "中文": "一个白人上这节目，张口就是一串刻板印象：我开车烂、视力差、还不能喝牛奶。",
    "英语": "A white person came on this show and just started rattling off stereotypes about us all.",
    "日语": "白人がこの番組に出てきて、私たちについての決めつけを並べ始めた。運転も悪いし視力も悪い。",
    "韩语": "백인이 이 프로그램에 나와서 우리에 대한 고정관념을 늘어놓기 시작했다. 운전도 못하고 시력도 나쁘다.",
    "泰语": "คนผิวขาวมาในรายการนี้แล้วเริ่มพูดถึงภาพจำเรื่องเรา ขับรถก็แย่ สายตาก็ไม่ดี",
    "阿拉伯语": "جاء شخص أبيض إلى هذا البرنامج وبدأ يردد الصور النمطية عنا، يقود السيارة بشكل سيء وبصره ضعيف.",
}
for lang, t in samples.items():
    def run(t=t):
        for _ in range(200):
            wrap_text(t + str(_), 18)   # 加序号避开 lru_cache
    ms = bench(run, 3) * 1000
    print(f"  {lang:6} {ms/200*1000:6.0f} µs/条")
