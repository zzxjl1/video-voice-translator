"""
命令的规则拦截层：只拦 **AI** 发起的命令（聊天里的 AI、以后 review 里的 omni），不拦用户。

为什么需要：模型给的参数是"生成"出来的 —— 它可以编一个时间，可以把咳嗽标成一整句话。这里的规则全部
基于**确定性的证据**（音轨能量、ASR 的词级时间戳、已有的行），不是"我觉得模型不该这样"。
每条规则只有三种结局：

  放行            参数没问题；
  改写            参数差一点：吸附到检测到的声音边界、对齐到分句边界。改写后照常执行，改了什么写进
                  结果的 `guard_notes`，模型看得到，不会以为自己的参数被原样采用；
  拒绝            参数站不住：带原因和建议回给模型，由它改正后重试（和命令自己抛的错同一个通道）。

没有"需要确认"这一档：确认是聊天面板的流程（`CONFIRM_*`），不在命令执行这一层。

**没有证据就不拦**：没有音轨、读不了、行的原文被改过没有词级信息，对应的规则直接跳过。规则是用来挡
明显的错，不是用来要求证据齐全。
"""
from typing import Callable, Optional

from app import asr_words
from app.commands.registry import CommandError, Ctx, resolve_line, resolve_lines
from app.services import audio_energy

VOCAL_MAX_S = 8.0               # 一段非语言声最长多久；更长的多半是一整句话，不是笑声
EVIDENCE_MIN_ACTIVE = 0.3       # 新插入的话，时间窗里至少这么多帧有声音
OVERLAP_EPS_S = 0.05            # 重叠不足这么多不算重叠（ASR 边界本来就有几十毫秒误差）
SPLIT_SNAP_S = 0.8              # 拆句的切点离分句边界多远以内可以吸附过去


class Reject(Exception):
    def __init__(self, error: str, hint: Optional[str] = None):
        super().__init__(error)
        self.error, self.hint = error, hint


_GUARDS: dict[str, list[Callable]] = {}


def guard(command_name: str):
    """注册一条规则：`fn(ctx, args, notes) -> args`。可以改 args（改写）、往 notes 里写说明、抛 Reject（拒绝）。"""
    def deco(fn):
        _GUARDS.setdefault(command_name, []).append(fn)
        return fn
    return deco


def run(ctx: Ctx, name: str, args: dict) -> tuple[dict, list[str]]:
    """依次过这条命令的所有规则。返回（可能被改写的参数，说明）；被拒绝时抛 Reject。"""
    notes: list[str] = []
    for fn in _GUARDS.get(name, []):
        out = fn(ctx, dict(args), notes)
        if out is not None:
            args = out
    return args, notes


# ----------------------------------------------------------------------------
# 共用的判断
# ----------------------------------------------------------------------------

def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def _speech_overlapping(state, start: float, end: float, exclude: set) -> list[int]:
    """和这个时间窗重叠的台词（kind=speech）的行号。"""
    return [i + 1 for i, s in enumerate(state.segments)
            if s.kind == "speech" and s.id not in exclude
            and _overlap(start, end, s.start_time, s.end_time) > OVERLAP_EPS_S]


def _nums(lines: list[int]) -> str:
    return "、".join(str(n) for n in lines[:6]) + ("…" if len(lines) > 6 else "")


_LEAK_HINT = ("想表现咳嗽、笑声：改那一句的原文，在对应位置加 [cough] / [laughing] 之类的标签，让配音去表现；"
              "或者把窗口缩到不和台词重叠的部分")


def _check_vocal_window(state, start: float, end: float, exclude: set) -> None:
    if end - start > VOCAL_MAX_S:
        raise Reject(f"一段非语言声不会长达 {end - start:.1f} 秒（上限 {VOCAL_MAX_S:.0f} 秒），这更像一整句话",
                     hint="如果其实是话，别标 vocal；如果是笑声夹着话，先 split_line 拆开")
    hits = _speech_overlapping(state, start, end, exclude)
    if hits:
        raise Reject(
            f"这段时间和第 {_nums(hits)} 句重叠 —— 那里有人在说话，保留这段原声会把原语言的人声一起带进成片，"
            "和配音叠在一起", hint=_LEAK_HINT)


# ----------------------------------------------------------------------------
# insert_line
# ----------------------------------------------------------------------------

@guard("insert_line")
def _insert_line(ctx: Ctx, args: dict, notes: list[str]) -> dict:
    state = ctx.state
    start, end = float(args["start"]), float(args["end"])
    total = audio_energy.audio_seconds(ctx.video_id)
    if total is not None and start >= total:
        raise Reject(f"起点 {start:.1f}s 超出了音频长度（{total:.1f}s）")

    if (args.get("kind") or "speech") == "vocal":
        _check_vocal_window(state, start, end, set())
        cands = audio_energy.find_candidates(ctx.video_id, state.segments)
        if cands is None:
            return args                                   # 没有音轨：没有证据，不拦
        best = max(cands, key=lambda c: _overlap(start, end, c["start"], c["end"]), default=None)
        if best is None or _overlap(start, end, best["start"], best["end"]) < 0.5 * (end - start):
            raise Reject("这段时间里没有\"ASR 漏掉的声音\"：要么根本没有声音，要么已经被某一句台词（或之前插入的行）覆盖了",
                         hint="先用 find_uncovered_sounds 看哪里有声音没被台词覆盖，再用 listen 听一下")
        new_start, new_end = best["start"], best["end"]
        if new_end - new_start > VOCAL_MAX_S:             # 候选比上限还长：只取你要的那部分
            new_start, new_end = max(new_start, start), min(new_end, end)
        if abs(new_start - start) > 0.02 or abs(new_end - end) > 0.02:
            notes.append(f"起止已吸附到检测到的声音边界：{start:.2f}–{end:.2f}s → {new_start:.2f}–{new_end:.2f}s")
            args["start"], args["end"] = new_start, new_end
        return args

    # 补一句话：时间窗里至少得有声音，不能凭空在静音里造一句
    frac = audio_energy.active_fraction(ctx.video_id, start, end)
    if frac is not None and frac < EVIDENCE_MIN_ACTIVE:
        raise Reject(f"这段时间里几乎没有声音（有声的部分只占 {frac:.0%}），不像有人在说话",
                     hint="先用 listen 听一下，或 find_uncovered_sounds 看哪里有声音")
    return args


# ----------------------------------------------------------------------------
# mark_non_speech
# ----------------------------------------------------------------------------

@guard("mark_non_speech")
def _mark_non_speech(ctx: Ctx, args: dict, notes: list[str]) -> dict:
    if args.get("kind") != "vocal":
        return args
    state = ctx.state
    try:
        segs = resolve_lines(state, args["lines"])
    except CommandError:
        return args                                       # 行号不对，让命令自己报错
    ids = {s.id for s in segs}
    for s in segs:
        _check_vocal_window(state, s.start_time, s.end_time, ids)
    return args


# ----------------------------------------------------------------------------
# split_line
# ----------------------------------------------------------------------------

@guard("split_line")
def _split_line(ctx: Ctx, args: dict, notes: list[str]) -> dict:
    state = ctx.state
    try:
        _, seg = resolve_line(state, args["line"])
    except CommandError:
        return args
    clauses = asr_words.clauses(ctx.video_id, seg)
    bounds = asr_words.boundaries(clauses) if clauses else []
    if not bounds:
        return args                                       # 没有词级信息（行被改过，或只有一个分句）：不管

    at = args.get("at_seconds")
    if at is not None:
        n, t = min(bounds, key=lambda b: abs(b[1] - float(at)))
        if abs(t - float(at)) > SPLIT_SNAP_S:
            shown = "、".join(f"第{k}分句后 {v:.2f}s" for k, v in bounds)
            raise Reject(f"切分时间 {float(at):.2f}s 不在这一句的任何分句边界附近。这一句的分句边界：{shown}",
                         hint="让切点落在分句之间；或者不给 at_seconds，由两半文字对应的分句来定")
        if abs(t - float(at)) > 0.02:
            notes.append(f"切点已对齐到分句边界：{float(at):.2f}s → {t:.2f}s（第 {n} 分句之后）")
            args["at_seconds"] = t
        return args

    # 没给时间：前半句文字恰好是前 N 个分句时，用 ASR 的词级时间戳，不再按字数比例估
    first = asr_words.normalize(args.get("text_first", ""))
    acc = ""
    for c, (n, t) in zip(clauses, bounds):
        acc += asr_words.normalize(c.text)
        if acc == first:
            args["at_seconds"] = t
            notes.append(f"切点按词级时间戳定在第 {n} 分句之后（{t:.2f}s）")
            break
    return args
