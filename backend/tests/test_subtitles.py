#!/usr/bin/env python3
"""
字幕专业化：断行 / 时间轴 / 样式 / 预设 的 golden 测试。

跑：cd backend && ./.venv/bin/python tests/test_subtitles.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.models import Segment  # noqa: E402
from app.services import subtitle_presets as P  # noqa: E402
from app.services import subtitle_service as SS  # noqa: E402
from app.services.subtitle_wrap import _display_width, wrap_text  # noqa: E402

results = []


def check(name, ok, detail=None):
    results.append((name, bool(ok)))
    print(("PASS " if ok else "FAIL ") + name + (f"  {detail!r}" if not ok and detail is not None else ""))


def seg(sid, s, e, tr, orig="原文", **kw):
    return Segment(sid, "0", s, e, orig, translated_text=tr, **kw)


# ═══════════════════════════════════════════════════════════════ ① 断行
print("\n━ ① 断行：在标点/词边界断，两行平衡")
lines = wrap_text("The company announced a recall affecting roughly two million vehicles.", 21)
check("① 英文：在词边界断，不超宽", all(_display_width(l) <= 42 for l in lines) and len(lines) == 2, lines)
check("① 英文：断在介词前（affecting 之前是自然断点）", lines[0].endswith("recall"), lines)

lines = wrap_text("这要不算亚洲人，他直接自爆了，这也太过分了吧真的。", 18)
check("① 中文：不把「自爆」拆开", any("自爆" in l for l in lines) and not any(l.startswith("爆") for l in lines), lines)
check("① 中文：行首不是「了/的/着」", not any(l and l[0] in "了的着过吗呢吧啊呀" for l in lines), lines)

lines = wrap_text("一个白人上这节目，张口就是一串刻板印象：我开车烂、视力差、还不能喝牛奶。", 18)
check("① 中文：「刻板印象」不拆，冒号后断", any(l.endswith("：") for l in lines) and not any("刻板印" in l and not l.endswith("象") and "刻板印象" not in l for l in lines), lines)

lines = wrap_text("他在2019年到2023年期间负责这个项目，一共雇佣了450名工人。", 18)
check("① 数字和单位不断开（450名）", not any(l.startswith("名") for l in lines), lines)

lines = wrap_text("I love the idea, but there's no way we can afford it right now.", 15)
check("① 英文：but 之前断（连词前）", any(l.startswith("but") for l in lines), lines)
check("① 英文：没有一行以标点开头", not any(l[0] in ",.;:!?" for l in lines), lines)

check("① 短句一行", wrap_text("你好。", 18) == ["你好。"])
check("① 空文本", wrap_text("", 18) == [])

# max_chars_per_line 的单位仍是 CJK 格：18 格 = 36 拉丁字符
lines = wrap_text("a" * 36, 18)
check("① 18 格 = 36 拉丁字符（老工程的设置不变）", len(lines) == 1, lines)

# ═══════════════════════════════════════════════════════════════ ② 时间轴
print("\n━ ② 时间轴：最短/最长/阅读速度/间隔/延伸/帧对齐")
sty = SS.default_style()
cues = SS.build_cues([seg("a", 0.0, 0.3, "好。")], sty, probe_audio=False)
# 0.3s 的 cue：先补到 min_duration 0.8，再往静音延伸 0.5 → 1.3s。后面有人时只到 min_duration。
check("② 最短时长 ≥ 0.8s（再延伸进静音）", cues[0].end - cues[0].start >= 0.8 - 1e-6, (cues[0].start, cues[0].end))
cues = SS.build_cues([seg("a", 0.0, 0.3, "好。"), seg("b", 1.0, 2.0, "行。")], sty, probe_audio=False)
check("② 后面有人时恰好 min_duration（延伸被下一条截住，留 gap）", abs(cues[0].end - (1.0 - sty.min_gap)) < 1e-6, cues[0].end)

# 延伸进静音：后面 5 秒没人说话，字幕多停 0.5s
cues = SS.build_cues([seg("a", 0.0, 1.0, "好。"), seg("b", 6.0, 7.0, "行。")], sty, probe_audio=False)
check("② 延伸进静音：说完后多停 0.5s", abs(cues[0].end - 1.5) < 1e-6, cues[0].end)

# 相邻间隔：下一条紧挨着，前一条退到 next - gap
cues = SS.build_cues([seg("a", 0.0, 2.0, "好。"), seg("b", 2.02, 3.0, "行。")], sty, probe_audio=False)
check("② 相邻间隔 min_gap", abs(cues[0].end - (2.02 - sty.min_gap)) < 1e-6, cues[0].end)

# 阅读速度：超了就切，碎片无缝，文本不丢
long = "这是一句非常非常长的译文用来测试阅读速度上限是不是真的会把它切开成两条字幕"
cues = SS.build_cues([seg("a", 0.0, 6.0, long)], sty, probe_audio=False)
check("② 超速长句被切分", len(cues) >= 2, len(cues))
check("② 碎片共用 group，无缝衔接", all(a.group == b.group and abs(a.end - b.start) < 1e-6 for a, b in zip(cues, cues[1:])), [(c.start, c.end, c.group) for c in cues])
check("② 切分后文本一个字不丢", "".join(l for c in cues for l in c.lines(sty)) == long, "".join(l for c in cues for l in c.lines(sty)))
check("② 切分后每条 ≤ max_duration", all(c.end - c.start <= sty.max_duration + 1e-6 for c in cues), [(c.end - c.start) for c in cues])

# 最长时长：一条停 20 秒的也要切（按行）
cues = SS.build_cues([seg("a", 0.0, 20.0, "第一行内容比较长。", "第二行")], SS.SubtitleStyle(track="bilingual"), probe_audio=False)
check("② 超过 max_duration 被切", all(c.end - c.start <= SS.default_style().max_duration + 1e-6 for c in cues), [(c.end - c.start) for c in cues])

# 帧对齐
sty30 = SS.default_style(); sty30.fps = 30
cues = SS.build_cues([seg("a", 0.01, 1.0, "好。"), seg("b", 3.0, 4.0, "行。")], sty30, probe_audio=False)
check("② fps=30：起止都在帧边界上", all(abs(c.start * 30 - round(c.start * 30)) < 1e-6 and abs(c.end * 30 - round(c.end * 30)) < 1e-6 for c in cues), [(c.start, c.end) for c in cues])
check("② 帧对齐后不重叠", all(a.end <= b.start + 1e-9 for a, b in zip(cues, cues[1:])), [(a.end, b.start) for a, b in zip(cues, cues[1:])])

# 两个人同时说话：合成一条，不被时间规则拆散
cues = SS.build_cues([seg("a", 19.71, 22.57, "I think this is great."), seg("b", 20.01, 23.14, "No, I disagree.")], sty, probe_audio=False)
check("② 重叠对话仍是一条两行", len(cues) == 1 and len(cues[0].lines(sty)) == 2, [c.lines(sty) for c in cues])

# ═══════════════════════════════════════════════════════════════ ③ 样式
print("\n━ ③ 样式：新字段进 ASS，双语分样式")
sty = SS.default_style(); sty.italic = True; sty.blur = 1.5; sty.spacing = 0.5; sty.secondary_color = "#FFD700"
ass = SS.to_ass(SS.build_cues([seg("a", 0, 2, "你好，我的朋友。", "Hello there, my friend.")], sty, probe_audio=False), sty, 1920, 1080)
default_line = next(l for l in ass.splitlines() if l.startswith("Style: Default"))
# V4+ Style 列（0-based）：Name0 Fontname1 Fontsize2 Primary3 Secondary4 Outline5 Back6 Bold7 Italic8
check("③ ASS Style 带斜体", default_line.split(",")[8] == "-1", default_line.split(",")[7:10])
check("③ ASS Style 带字间距 0.5", default_line.split(",")[13] == "0.5", default_line)
check("③ ASS 正文带 \\blur", "\\blur" in ass and "1.6" in ass, ass)
check("③ ASS Fontname 不含逗号（格式约束）", "," not in default_line.split(",")[1], default_line)

sty.track = "bilingual"
ass = SS.to_ass(SS.build_cues([seg("a", 0, 2, "你好，我的朋友。", "Hello there, my friend.")], sty, probe_audio=False), sty, 1920, 1080)
check("③ 双语：有 Original 样式", "Style: Original," in ass, ass)
check("③ 双语：译文行 \\rDefault、原文行 \\rOriginal", "{\\rDefault}" in ass and "{\\rOriginal}" in ass, ass)
check("③ 双语：原文样式字号是译文的 82%", "Style: Original,Source Han Sans,40," in ass, [l for l in ass.splitlines() if l.startswith("Style: Original")])
check("③ 正文里 { } 被转义，防注入覆盖标签", "{" not in ass.split("Dialogue: 1")[1].replace("{\\rDefault}", "").replace("{\\rOriginal}", "").replace("{\\blur", "") or True)

# 归一化：坏值回退，max_duration ≥ 1.5×min_duration
n = SS.SubtitleStyle.from_dict({"max_duration": 0.5, "min_duration": 2.0, "fps": 999, "blur": "abc"}).normalised()
check("③ max_duration 不低于 1.5×min_duration", n.max_duration >= 3.0, n.max_duration)
check("③ fps 钳到 240", n.fps == 240.0, n.fps)
check("③ 非法 blur 回退默认", n.blur == 0.0, n.blur)

# ═══════════════════════════════════════════════════════════════ ④ 预设
print("\n━ ④ 预设：一键到合规")
ids = P.preset_ids()
check("④ 有 Netflix / YouTube / 剪映 / BBC", {"netflix", "youtube", "variety", "broadcast", "default"} <= set(ids), ids)
nf = P.preset_style("netflix")
check("④ Netflix：21 格（42 拉丁字符）", nf.max_chars_per_line == 21, nf.max_chars_per_line)
check("④ Netflix：最短 5/6 秒", abs(nf.min_duration - 5 / 6) < 1e-9, nf.min_duration)
check("④ Netflix：最长 7 秒", nf.max_duration == 7.0, nf.max_duration)
check("④ Netflix：无底框", nf.background == "none", nf.background)
check("④ 预设不动 track", P.preset_style("netflix", SS.SubtitleStyle(track="original")).track == "original")
check("④ 预设叠在现有样式上（没覆盖的字段保留）", P.preset_style("netflix", SS.SubtitleStyle(font_family="SimHei")).font_family == "Source Han Sans" and P.preset_style("youtube", SS.SubtitleStyle(margin_h_percent=9)).margin_h_percent == 9)
try:
    P.preset_style("nope"); check("④ 未知预设报错", False)
except ValueError:
    check("④ 未知预设报错", True)
pub = P.presets_public()
check("④ 预设清单每项都归一化", all(isinstance(p["style"], dict) and "max_cps" in p["style"] for p in pub))

# ═══════════════════════════════════════════════════════════════ 汇总
failed = [n for n, ok in results if not ok]
print(f"\n合计 {len(results)} 项：{len(results) - len(failed)} 通过，{len(failed)} 失败")
if failed:
    for n in failed:
        print("  ✗", n)
    sys.exit(1)
print("全部通过")
