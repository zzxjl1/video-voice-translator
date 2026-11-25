#!/usr/bin/env python3
"""
用真实字幕文件做全面测试。

素材（本机 ~/Downloads，不进 git）：
  · School.Spirits S03E01–03 中文字幕组版（~3000 条，32% 多行）—— 专业字幕编辑断好的行
  · 微信视频…_en.srt —— 机翻英译，每条 10 秒长句，压阅读速度/切分

三类测试：
  ① 断行 vs 专业字幕：把专业字幕的**多行条合并成一句**，再用我们的算法断回去，比对断点是否一致
  ② 时间轴：真实 cue 流过 build_cues，检查不重叠 / gap / 最短最长 / 阅读速度，并统计改变了多少
  ③ 长英文：每条 10 秒的机翻英文，看切分是否合理、文本不丢

跑：cd backend && ./.venv/bin/python tests/test_subtitles_corpus.py
"""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Segment  # noqa: E402
from app.services import subtitle_service as SS  # noqa: E402
from app.services.subtitle_wrap import _display_width, wrap_text  # noqa: E402

DL = Path.home() / "Downloads"
FILES = sorted(DL.glob("School.Spirits*.srt"))
EN_FILE = DL / "微信视频2025-11-08_214157_900__en.srt"


def parse_srt(path: Path) -> list[dict]:
    t = path.read_text(encoding="utf-8-sig", errors="replace")
    out = []
    for b in re.split(r"\n\s*\n", t.strip()):
        if "-->" not in b:
            continue
        lines = b.split("\n")
        idx = next(i for i, l in enumerate(lines) if "-->" in l)
        m = re.match(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", lines[idx])
        if not m:
            continue
        g = list(map(int, m.groups()))
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000
        text = [l.strip() for l in lines[idx + 1:] if l.strip()]
        out.append({"start": s, "end": e, "lines": text, "text": " ".join(text)})
    return out


results = []


def check(name, ok, detail=None):
    results.append((name, bool(ok)))
    if not ok:
        print(f"FAIL {name}  {detail!r}" if detail is not None else f"FAIL {name}")


if not FILES:
    print(f"no corpus: put School.Spirits*.srt in {DL}")
    sys.exit(2)

cues = [c for f in FILES for c in parse_srt(f)]
multi = [c for c in cues if len(c["lines"]) > 1]
print(f"语料：{len(FILES)} 个文件，{len(cues)} 条，{len(multi)} 条多行")

# ─────────────────────────────────────────────────────────── ① 断行 vs 专业字幕
print("\n━ ① 断行：专业字幕的两行条，合并后用我们的算法断回去")
widths = sorted(_display_width(l) for c in multi for l in c["lines"])
print(f"  专业字幕单行宽度：中位 {widths[len(widths)//2]:.0f}（≈{widths[len(widths)//2]/2:.0f} 汉字），95 分位 {widths[int(len(widths)*0.95)]:.0f}")
# 排除双人对话（- 开头是刻意的两行）、省略号/括号开头（原文本身的形状）
wrap_cases = [c for c in multi if len(c["lines"]) == 2
              and not any(l.startswith(("-", "…", "—", "[", "♪", "（", "(")) for l in c["lines"])]
import jieba.posseg as pseg  # noqa: E402
import jieba  # noqa: E402
jieba.setLogLevel(60)

NOUN = set("n nr ns nt nz s nrt nw an".split())
PRON = {"r"}
OK_TAIL = set("v vn vd vg a ad an b d t tg uj ul p c".split())
BAD_TAIL = set("x y e o u ul".split())
def _break_is_grammatical(line: str) -> bool:
    """一行的结尾是不是专业字幕会接受的断点：标点、名词/代词、谓语/修饰语。"""
    tail = line.rstrip()[-1:]
    if tail in "，。！？；：、…—":
        return True
    cut = list(pseg.lcut(line.rstrip()))
    pos = cut[-1].flag if cut else ""
    return pos in NOUN or pos in PRON or pos in OK_TAIL


verdict: dict[str, int] = {}
examples: dict[str, tuple] = {}
for c in wrap_cases:
    a, b = c["lines"]
    joined = (a + " " + b) if (" " in a or " " in b) else (a + b)
    cap = max(_display_width(a), _display_width(b)) / 2 + 0.6
    ours = wrap_text(joined, cap)
    if len(ours) != 2:
        # 3 行不是失败：专业字幕为了守"最多 2 行"会把词拆开（"穿着|渔夫背心"），
        # 我们守语法、多出一行——真实管线里它会切成两条连续字幕。只要求每个断点都合法。
        k = "≥3 行（断点合法）" if all(
            _break_is_grammatical(x) for x in ours[:-1]) else "≥3 行（有坏断点）"
    elif re.sub(r"\s", "", ours[0]) == re.sub(r"\s", "", a):
        k = "完全一致"
    else:
        tail = ours[0].rstrip()[-1:]
        cut = list(pseg.lcut(ours[0].rstrip()))
        pos = cut[-1].flag if cut else ""
        if tail in "，。！？；：、…—": k = "断在标点后"
        elif pos in NOUN or pos in PRON: k = "断在主语/名词后"
        elif pos in OK_TAIL: k = "断在谓语/修饰语后"
        elif pos in BAD_TAIL: k = "断在虚词后（差）"
        else: k = f"其它({pos})"
    verdict[k] = verdict.get(k, 0) + 1
    examples.setdefault(k, (a, b, ours))
tot = sum(verdict.values())
good = sum(v for k, v in verdict.items() if k in ("完全一致", "断在主语/名词后", "断在标点后"))
bad = verdict.get("断在虚词后（差）", 0)
print(f"  {tot} 条：完全一致 {verdict.get('完全一致',0)} | 名词后 {verdict.get('断在主语/名词后',0)} | 标点后 {verdict.get('断在标点后',0)} → 专业级 {100*good/tot:.1f}%")
print(f"           谓语后 {verdict.get('断在谓语/修饰语后',0)} | ≥3行合法 {verdict.get('≥3 行（断点合法）',0)} | ≥3行有坏断点 {verdict.get('≥3 行（有坏断点）',0)} | 虚词后 {bad}")
for k in ("断在虚词后（差）", "≥3 行（有坏断点）"):
    if k in examples:
        a, b, o = examples[k]; print(f"    [{k}] 专业: {a!r} | {b!r}   我们: {o}")
check("① 专业级断点（一致/名词后/标点后）≥ 80%", good / tot >= 0.80, good / tot)
check("① 断在虚词后（差）≤ 2%", bad / tot <= 0.02, bad / tot)
check("① ≥3 行时每个断点都合法（坏断点 ≤ 2%）", verdict.get("≥3 行（有坏断点）", 0) / tot <= 0.02, verdict.get("≥3 行（有坏断点）", 0))

# ① b 硬规则：行首不是收尾标点/后缀虚词
bad_start = 0; bad_examples = []
for c in wrap_cases:
    joined = " ".join(c["lines"]) if (" " in c["text"]) else "".join(c["lines"])
    cap = max(_display_width(l) for l in c["lines"]) / 2 + 0.5
    for l in wrap_text(joined, cap):
        if l and (l[0] in "，。！？；：、）》」』】”’…,.;:!?)]" or l[0] in "了的着过吗呢吧啊呀"):
            bad_start += 1
            if len(bad_examples) < 3: bad_examples.append(l)
check("① 行首不是收尾标点/虚词（专业字幕的硬规则）", bad_start == 0, bad_examples)

# ① c 不丢字
lost = 0
for c in wrap_cases:
    joined = "".join(c["lines"]).replace(" ", "")
    cap = max(_display_width(l) for l in c["lines"]) / 2 + 0.5
    ours = "".join(wrap_text(" ".join(c["lines"]), cap)).replace(" ", "")
    if ours != joined:
        lost += 1
check("① 断行不丢字", lost == 0, lost)

# ─────────────────────────────────────────────────────────── ② 时间轴
print("\n━ ② 时间轴：真实 cue 流过 build_cues")
for f in FILES:
    cs = parse_srt(f)
    # 只取普通对话条做 segment（双人对话条也进，它本来就该合一条）
    segs = [Segment(f"s{i}", "spk" + ("0" if i % 2 else "1"), c["start"], c["end"], c["text"], translated_text=c["text"])
            for i, c in enumerate(cs)]
    for label, sty in (("默认", SS.default_style()), ("Netflix", None)):
        if sty is None:
            from app.services import subtitle_presets as P
            sty = P.preset_style("netflix")
        out = SS.build_cues(segs, sty, probe_audio=False)
        n_in, n_out = len(cs), len(out)
        # 不重叠
        overlaps = sum(1 for a, b in zip(out, out[1:]) if a.end > b.start + 1e-6 and a.group != b.group)
        # 最短（后面有人才需要钳；最后一条延伸后一定够）
        too_short = sum(1 for c in out if c.end - c.start < sty.min_duration - 1e-6 and (out.index(c) + 1 < len(out)))
        too_long = sum(1 for c in out if c.end - c.start > sty.max_duration + 1e-6)
        # 阅读速度（只算译文）
        fast = 0
        for c in out:
            d = c.end - c.start
            if d > 0 and SS._cue_cells(c, sty) / d > sty.max_cps * 1.001:
                fast += 1
        # 文本守恒
        norm = lambda x: re.sub(r"[\s\u200b-]", "", x)
        src = norm("".join(c["text"] for c in cs))
        dst = norm("".join(l for c in out for l in c.lines(sty)))
        print(f"  {f.name[:22]}… [{label}] {n_in} → {n_out} 条 | 重叠 {overlaps} | 过短 {too_short} | 过长 {too_long} | 超速 {fast} | 文本{'守恒' if src == dst else '不守恒'}")
        check(f"② {label} {f.name[:14]} 不重叠", overlaps == 0, overlaps)
        check(f"② {label} {f.name[:14]} 不过长", too_long == 0, too_long)
        check(f"② {label} {f.name[:14]} 文本守恒", src == dst, (len(src), len(dst)))

# ─────────────────────────────────────────────────────────── ③ 长英文
print("\n━ ③ 机翻长英文：每条 10 秒，压阅读速度")
cs = parse_srt(EN_FILE)
segs = [Segment(f"e{i}", "0", c["start"], c["end"], c["text"], translated_text=c["text"]) for i, c in enumerate(cs)]
for label, sty in (("默认(9cps)", SS.default_style()), ("Netflix(8.5cps)", None)):
    if sty is None:
        from app.services import subtitle_presets as P
        sty = P.preset_style("netflix")
    out = SS.build_cues(segs, sty, probe_audio=False)
    fast = sum(1 for c in out if (c.end - c.start) > 0 and SS._cue_cells(c, sty) / (c.end - c.start) > sty.max_cps * 1.001)
    mx = max(len(c.lines(sty)) for c in out)
    norm = lambda x: re.sub(r"[\s\u200b-]", "", x)
    src = norm("".join(c["text"] for c in cs))
    dst = norm("".join(l for c in out for l in c.lines(sty)))
    print(f"  [{label}] {len(cs)} → {len(out)} 条 | 仍超速 {fast} | 最多 {mx} 行 | 文本{'守恒' if src == dst else '不守恒'}")
    print("  样例：")
    for c in out[:3]:
        for l in c.lines(sty):
            print(f"      {c.start:6.2f}  {l}")
    check(f"③ {label} 不超 max_lines 行", mx <= sty.max_lines, mx)
    check(f"③ {label} 文本守恒", src == dst)
    # 源是 10 秒一条的机翻长句（本身 20 cps），时间不够是源的物理限制。真正要验证的
    # 不变量：① 切出来的每条 ≤ max_lines 行 ② 文本守恒 ③ 时间分配公平——同一源 cue 的
    #    碎片按 cells 比例分，每条要么 ≥ need，要么和同组其它碎片一样"按比例缩水"。
    #    换句话说：超速只能是"整组一起超"，不能是"某一条被饿死、另一条撑着"。
    by_group: dict[str, list] = {}
    for c in out:
        by_group.setdefault(c.group or f"s{c.start}", []).append(c)
    starved = 0
    for g, cs_ in by_group.items():
        if len(cs_) < 2:
            continue
        total_cells = sum(SS._cue_cells(c, sty) for c in cs_)
        total_dur = sum(c.end - c.start for c in cs_)
        for c in cs_:
            fair = total_dur * SS._cue_cells(c, sty) / total_cells
            actual = c.end - c.start
            # 实际时长和"按 cells 比例应得的"差不到 15%（留出 min_duration 钳制的余量）
            if actual < fair * 0.85:
                starved += 1
    check(f"③ {label} 碎片按阅读量公平分配时间（饿死 0 条）", starved == 0, starved)
    check(f"③ {label} 超速时整组一起超（无单条饿死）", all(
        (c.end - c.start) >= SS._cue_cells(c, sty) / sty.max_cps * 0.6 for c in out))

# ─────────────────────────────────────────────────────────── 汇总
failed = [n for n, ok in results if not ok]
print(f"\n合计 {len(results)} 项：{len(results) - len(failed)} 通过，{len(failed)} 失败")
if failed:
    for n in failed:
        print("  ✗", n)
    sys.exit(1)
print("全部通过")
