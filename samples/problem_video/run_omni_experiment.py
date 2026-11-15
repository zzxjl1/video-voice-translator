#!/usr/bin/env python3
"""
实验：omni 在"全听 review"这一轮里，能不能（1）发现 ASR 吞掉/并掉的重叠发言，（2）认出 ASR 空隙里的非语言声，
（3）只用"第几分句"这种编号来定位 —— 不输出任何秒数。

输入全部来自样本里已有的真实 ASR 基线（asr_baseline/）：
  · 台词 = 人声轨那次的真实 ASR 输出（7 句；第 5 句把两个人的话并在了一起，B 的反驳整句丢了）
  · 分句 = 把 ASR 的词级时间戳按标点切出来，编号给 omni
  · 空隙候选 = 服务端在人声轨上找"有声音、但没被台词覆盖"的区间（纯能量，样本里 4 个事件 100% 命中）

两种配置各调用一次：
  original  omni 听原声（含风声、撞击、音乐）—— 设计里推荐的生产配置
  vocals    omni 听人声轨 —— 对照：看听原声有没有帮助、或帮倒忙

每次调用的提示、原始回答、token 用量都存到 omni_experiment/（不含 API key、不含音频）。
只调用 omni 两次（音频各约 35 秒）。用法：  python3 run_omni_experiment.py
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", "..", "backend"))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
from dotenv import dotenv_values  # noqa: E402

for k, v in dotenv_values(os.path.join(BACKEND, ".env")).items():
    os.environ.setdefault(k, v)

from app import config  # noqa: E402
from app.services import omni_service  # noqa: E402

SR = 16000
OUT = os.path.join(HERE, "omni_experiment")
KINDS = "speech | laugh | sigh | cough | breath | wind | impact | music | noise | other | none"


# ----------------------------------------------------------------------------- 输入的准备

def ffmpeg_pcm(path):
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
                       capture_output=True, check=True)
    return r.stdout


def wav_b64(pcm: bytes) -> str:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


def load_asr_lines():
    """人声轨那次真实 ASR：每句带分句（按词级标点切）。"""
    doc = json.load(open(os.path.join(HERE, "asr_baseline", "asr_vocals.json"), encoding="utf-8"))
    sentences = doc["raw_transcripts"][0]["transcripts"][0]["sentences"]
    lines = []
    for i, s in enumerate(sentences, 1):
        clauses, cur = [], []
        for w in s["words"]:
            cur.append(w)
            if w.get("punctuation"):
                clauses.append(cur); cur = []
        if cur:
            clauses.append(cur)
        lines.append({
            "id": f"seg-{i:02d}", "start": s["begin_time"] / 1000, "end": s["end_time"] / 1000, "text": s["text"],
            "clauses": [{"n": n, "text": "".join(w["text"] + w.get("punctuation", "") for w in c),
                         "t0": c[0]["begin_time"] / 1000, "t1": c[-1]["end_time"] / 1000}
                        for n, c in enumerate(clauses, 1)],
        })
    return lines


def energy_runs(x, margin_db=12.0, win=0.04, hop=0.02, min_on=0.10, max_gap=0.15):
    w, h = int(win * SR), int(hop * SR)
    db = np.array([20 * np.log10(np.sqrt(np.mean(x[i:i + w] ** 2)) + 1e-9) for i in range(0, len(x) - w, h)])
    on = db > np.percentile(db, 10) + margin_db
    runs, s = [], None
    for i, v in enumerate(on):
        if v and s is None:
            s = i
        if not v and s is not None:
            runs.append([s * hop, i * hop]); s = None
    if s is not None:
        runs.append([s * hop, len(on) * hop])
    merged = []
    for r in runs:
        if merged and r[0] - merged[-1][1] < max_gap:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [r for r in merged if r[1] - r[0] >= min_on]


def find_candidates(vocals_pcm, lines):
    """有声音、但没被台词覆盖 >0.3s 的区间；每个标出"在第几句之后"。"""
    x = np.frombuffer(vocals_pcm, dtype=np.int16).astype(np.float64) / 32768
    out = []
    for s, e in energy_runs(x):
        covered = sum(max(0.0, min(e, l["end"]) - max(s, l["start"])) for l in lines)
        if (e - s) - covered > 0.3:
            before = [l for l in lines if l["end"] <= s + 0.05]
            out.append({"start": round(s, 2), "end": round(e, 2), "after": before[-1]["id"] if before else None})
    for c in out:
        same = [d for d in out if d["after"] == c["after"]]
        c["nth"], c["of"] = same.index(c) + 1, len(same)
    for i, c in enumerate(out, 1):
        c["h"] = f"H{i}"
    return out


# ----------------------------------------------------------------------------- 提示

def build_prompt(lines, cands):
    listing = "\n".join(f'{l["id"]}: ' + "".join(f'[{c["n"]}]{c["text"]}' for c in l["clauses"]) for l in lines)
    marks = "\n".join(
        f'{c["h"]}: after line {c["after"]}'
        + (f' ({c["nth"]} of {c["of"]} in that pause)' if c["of"] > 1 else "")
        + f', about {c["end"] - c["start"]:.1f}s long' for c in cands) or "(none)"
    system = (
        "You are a transcription reviewer. You HEAR the original video audio and read the ASR transcript of the "
        "same video. ASR transcribes ONE speaker per line and cannot handle people talking at the same time.\n\n"
        "Each line below is split into numbered clauses like [1]...[2]... . Line boundaries and clause numbers are "
        "GIVEN. NEVER output timestamps or seconds. Refer to places only by line id and clause number.\n\n"
        "Do three things:\n"
        "1. PROOFREAD every line against what you hear (fix mis-recognized words; keep the original language; do not "
        "translate; do not renumber or re-split). Return the corrected text of the whole line, clause numbers removed.\n"
        "2. STRUCTURAL PROBLEMS inside a line — report ONLY what you clearly hear, otherwise leave ops empty:\n"
        '   {"op":"split","after_clause":N}  the line is really two different speakers taking turns; the second '
        "speaker starts right after clause N.\n"
        '   {"op":"overlap","text":"...","from_clause":A,"to_clause":B}  another person talks AT THE SAME TIME as the '
        "line's speaker during clauses A..B, and ASR dropped or merged those words. text = what that person said, in the "
        "original language.\n"
        '   {"op":"vocal","kind":"laugh|sigh|cough|breath","after_clause":N}  a human non-speech sound in the pause '
        "right after clause N.\n"
        "3. GAP MARKERS: the voice track has sound at these places where ASR transcribed nothing. For each marker say "
        f"what the sound IS. kind is one of: {KINDS}. If it is speech, give the words in text. Use none if you hear "
        "nothing there. (The audio you hear may also contain background sound such as wind or music; report the sound "
        "that is actually at that place.)\n\n"
        'Reply with a JSON object only: {"lines":[{"id":"...","text":"...","ops":[]}],'
        '"gaps":[{"h":"H1","kind":"...","text":""}]}. Every line id must appear exactly once. ops is usually empty.'
    )
    return f"{system}\n\nLines:\n{listing}\n\nGap markers:\n{marks}"


# ----------------------------------------------------------------------------- 调用与评估

async def ask(prompt, audio_b64):
    t0 = time.perf_counter()
    r = await omni_service._client.chat.completions.create(
        model=config.OMNI_MODEL,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "input_audio", "input_audio": {"data": f"data:;base64,{audio_b64}", "format": "wav"}},
        ]}],
        response_format={"type": "json_object"},
        extra_body={"enable_thinking": False},
    )
    return r.choices[0].message.content or "", (r.usage.model_dump() if r.usage else {}), time.perf_counter() - t0


def jaccard(a, b):
    a, b = set(a), set(b)
    return len(a & b) / max(1, len(a | b))


def evaluate(name, lines, cands, raw, verbose=True):
    p = print if verbose else (lambda *a, **k: None)
    truth = json.load(open(os.path.join(HERE, "truth.json"), encoding="utf-8"))
    events = [e for e in truth["events"] if e["category"] != "overlap"]
    byid = {l["id"]: l for l in lines}
    p(f"\n{'─' * 70}\n配置 {name}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        p("  !! 回答不是合法 JSON：", raw[:300]); return {"valid": False}
    got = {str(x.get("id")): x for x in data.get("lines", []) if isinstance(x, dict)}
    p(f"  每句都回了：{set(got) == set(byid)}（缺 {sorted(set(byid) - set(got))}，多 {sorted(set(got) - set(byid))}）")

    p("  ── 空隙标记（真值来自 truth.json）")
    gaps = {str(g.get("h")): g for g in data.get("gaps", []) if isinstance(g, dict)}
    gap_hits = 0
    gap_kinds = []
    for c in cands:
        g = gaps.get(c["h"], {})
        want = [e.get("label") for e in events if e["category"] == "nonverbal_human"
                and min(c["end"], e["end"]) - max(c["start"], e["start"]) > 0.05]
        ok = bool(want) and g.get("kind") == want[0]
        gap_hits += ok
        gap_kinds.append(g.get("kind"))
        p(f'     {c["h"]} {c["start"]:5.2f}-{c["end"]:5.2f}  真值={want}  omni={g.get("kind")!r} {g.get("text", "")!r}  {"OK" if ok else "不符"}')

    p("  ── 行内结构化标注")
    n_ops = 0
    split_ok = overlap_ok = cough_ok = False
    false_pos = []
    for lid, x in got.items():
        for op in x.get("ops") or []:
            n_ops += 1
            where = byid.get(lid)
            if not where:
                p(f"     ?? 未知行 {lid}: {op}"); continue
            cl = {c["n"]: c for c in where["clauses"]}
            kind = op.get("op")
            if kind == "split":
                n = op.get("after_clause"); t = cl.get(n, {}).get("t1")
                p(f"     {lid} split after_clause={n} → 约 {t} 秒（真值：B 在 19.21 结束，A 在 19.71 开始）")
                split_ok |= (lid == "seg-05" and t is not None and 18.9 <= t <= 19.8)
            elif kind == "overlap":
                a, b = op.get("from_clause"), op.get("to_clause")
                t0, t1 = cl.get(a, {}).get("t0"), cl.get(b, {}).get("t1")
                sim = jaccard(op.get("text", ""), "不对，我完全不同意，成本太高了。")
                p(f"     {lid} overlap clauses {a}-{b} → 约 {t0}-{t1} 秒，text={op.get('text')!r}（与真句字符相似度 {sim:.0%}；真值 20.01-23.14）")
                if lid == "seg-05" and t0 is not None and t1 is not None:
                    inter = max(0.0, min(t1, 23.14) - max(t0, 20.01))
                    overlap_ok |= inter / (23.14 - 20.01) > 0.5 and sim > 0.5
            elif kind == "vocal":
                n = op.get("after_clause"); t = cl.get(n, {}).get("t1")
                p(f"     {lid} vocal {op.get('kind')} after_clause={n} → 约 {t} 秒（真值：咳嗽 17.93-18.29）")
                cough_ok |= (lid == "seg-05" and op.get("kind") == "cough" and t is not None and 17.5 <= t <= 18.4)
            else:
                p(f"     {lid} 未知 op：{op}")
            if lid != "seg-05":
                false_pos.append((lid, op))
    if n_ops == 0:
        p("     （没有任何 op）")

    p("  ── 校对文字有没有被改坏（和 ASR 原文逐句对比）")
    changed = [(lid, byid[lid]["text"], x.get("text")) for lid, x in got.items()
               if lid in byid and x.get("text") and x["text"] != byid[lid]["text"] and lid != "seg-05"]
    for lid, a, b in changed:
        p(f"     {lid}: {a!r} → {b!r}")
    if not changed:
        p("     除第 5 句外，其余原样返回")

    p(f"\n  小结 {name}：空隙 {gap_hits}/{len(cands)}　拆句={'OK' if split_ok else '没找到'}　"
          f"补上重叠发言={'OK' if overlap_ok else '没找到'}　咳嗽={'OK' if cough_ok else '没找到'}　"
          f"其他行上的多余 op={len(false_pos)} {false_pos[:2]}")
    return {"valid": True, "gap_hits": gap_hits, "gap_total": len(cands), "gap_kinds": gap_kinds, "split_ok": split_ok,
            "overlap_ok": overlap_ok, "cough_ok": cough_ok, "false_pos": len(false_pos), "changed_other": len(changed)}


async def main():
    if not config.DASHSCOPE_API_KEY:
        raise SystemExit("backend/.env 里没有 DASHSCOPE_API_KEY")
    repeat = int(sys.argv[sys.argv.index("--repeat") + 1]) if "--repeat" in sys.argv else 1
    os.makedirs(OUT, exist_ok=True)
    lines = load_asr_lines()
    vocals = ffmpeg_pcm(os.path.join(HERE, "speech_stem.flac"))
    cands = find_candidates(vocals, lines)
    prompt = build_prompt(lines, cands)
    print(f"模型 {config.OMNI_MODEL}；台词 {len(lines)} 句，空隙候选 {len(cands)} 个：",
          [(c["h"], c["start"], c["end"], c["after"]) for c in cands])
    print(f"提示共 {len(prompt)} 字符；每个配置重复 {repeat} 次")
    audios = {"original": wav_b64(ffmpeg_pcm(os.path.join(HERE, "problem_sample.mp4"))), "vocals": wav_b64(vocals)}
    results = {name: [] for name in audios}
    for i in range(1, repeat + 1):
        for name, audio in audios.items():
            try:
                raw, usage, secs = await ask(prompt, audio)
            except Exception as e:  # 实验脚本：失败要看见原因
                print(f"[{name} #{i}] 调用失败：{type(e).__name__}: {str(e)[:300]}")
                continue
            with open(os.path.join(OUT, f"omni_{name}_{i}.json"), "w", encoding="utf-8") as f:
                json.dump({"model": config.OMNI_MODEL, "prompt": prompt, "raw_response": raw, "usage": usage,
                           "seconds": round(secs, 2), "candidates": cands}, f, ensure_ascii=False, indent=2)
            det = usage.get("prompt_tokens_details") or {}
            m = evaluate(name, lines, cands, raw, verbose=(repeat == 1)) or {}
            m.update(prompt_tokens=usage.get("prompt_tokens"), audio_tokens=det.get("audio_tokens"),
                     out_tokens=usage.get("completion_tokens"), seconds=round(secs, 1))
            results[name].append(m)
            print(f"[{name} #{i}] 空隙 {m.get('gap_hits')}/{m.get('gap_total')} {m.get('gap_kinds')}  拆句={m.get('split_ok')}  "
                  f"补重叠={m.get('overlap_ok')}  咳嗽={m.get('cough_ok')}  多余op={m.get('false_pos')}  其余行被改={m.get('changed_other')}  "
                  f"token {m['prompt_tokens']}+{m['out_tokens']}  {m['seconds']}s")
    if repeat > 1:
        print("\n══ 汇总 ══")
        for name, rs in results.items():
            ok = [r for r in rs if r.get("valid")]
            n = len(rs)
            if not n:
                continue
            print(f"{name:<9}次数 {n}  合法JSON {len(ok)}  拆句 {sum(r['split_ok'] for r in ok)}/{n}  "
                  f"补上重叠发言 {sum(r['overlap_ok'] for r in ok)}/{n}  咳嗽 {sum(r['cough_ok'] for r in ok)}/{n}  "
                  f"空隙命中 {sum(r['gap_hits'] for r in ok)}/{sum(r['gap_total'] for r in ok)}  "
                  f"多余op共 {sum(r['false_pos'] for r in ok)}  其余行被改共 {sum(r['changed_other'] for r in ok)}  "
                  f"平均 token {sum(r['prompt_tokens'] for r in ok) // max(1, len(ok))}+{sum(r['out_tokens'] for r in ok) // max(1, len(ok))}")


if __name__ == "__main__":
    asyncio.run(main())
