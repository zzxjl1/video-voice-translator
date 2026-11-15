"""
ASR 的词级时间戳：旁路文件 `asr_words.json`。

为什么单独放、不进 `Segment`：词级时间戳是 ASR 返回的**不可变**事实，只服务于"在一句话内部
定位"（拆句、补上被吞掉的发言）。放进 `Segment` 的话，改原文、改时间、拆句、合并每个编辑
命令都要记得维护它。旁路文件把维护成本换成一条校验：**这一行的原文和 ASR 当时给的原文一模一样
才采用**（见 `clauses`），改过的行自然查不到，不需要任何失效逻辑。

分句 = 按 ASR 给每个词附带的标点切开。分句的边界天然落在说话的停顿处，精度在一两秒，
正好够"这一句其实是两个人轮流说的"这类判断。
"""
import os
import re
from dataclasses import dataclass
from typing import Optional

from app.models import atomic_write_json, video_dir_path

WORDS_FILE = "asr_words.json"
_PUNCT = re.compile(r"[\W_]+", re.UNICODE)


@dataclass
class Clause:
    n: int              # 1 起
    text: str           # 含标点
    t0: float
    t1: float


def _path(video_id: str) -> str:
    return os.path.join(video_dir_path(video_id), WORDS_FILE)


def save(video_id: str, lines: list[dict]) -> None:
    """`lines` 是 `asr_service.parse_asr_results` 的输出。没有词级信息的行不存。"""
    data = {l["id"]: {"text": l["text"], "words": l["words"]} for l in lines if l.get("words")}
    if data:
        atomic_write_json(_path(video_id), data)


def load(video_id: str) -> dict:
    import json
    try:
        with open(_path(video_id), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def split_clauses(words: list[dict]) -> list[Clause]:
    """按词自带的标点切分句。最后一段没有标点也算一个分句。"""
    clauses: list[Clause] = []
    cur: list[dict] = []
    for w in words:
        cur.append(w)
        if (w.get("p") or "").strip():
            clauses.append(_make(len(clauses) + 1, cur))
            cur = []
    if cur:
        clauses.append(_make(len(clauses) + 1, cur))
    return clauses


def _make(n: int, ws: list[dict]) -> Clause:
    return Clause(n=n, text="".join((w.get("t") or "") + (w.get("p") or "") for w in ws),
                  t0=float(ws[0]["b"]), t1=float(ws[-1]["e"]))


def clauses(video_id: str, seg) -> Optional[list[Clause]]:
    """这一行的分句；行的原文被改过（或根本没有词级信息）时返回 None。"""
    rec = load(video_id).get(seg.id)
    if not rec or rec.get("text") != seg.text:
        return None
    out = split_clauses(rec.get("words") or [])
    return out or None


def boundaries(cl: list[Clause]) -> list[tuple[int, float]]:
    """相邻分句之间的切点 [(n, 秒)]：n 表示"切在第 n 分句之后"。取两个分句之间空隙的中点，不会切到词里面。"""
    return [(c.n, round((c.t1 + nxt.t0) / 2, 3)) for c, nxt in zip(cl, cl[1:])]


def window(cl: list[Clause], first: int, last: int) -> Optional[tuple[float, float]]:
    """第 first 到第 last 分句占的时间窗；编号不合法返回 None。"""
    by_n = {c.n: c for c in cl}
    if first not in by_n or last not in by_n or first > last:
        return None
    return by_n[first].t0, by_n[last].t1


def normalize(text: str) -> str:
    """去掉空白和标点，只留文字：比较"这半句是不是恰好是前几个分句"时用。"""
    return _PUNCT.sub("", text or "")
