"""
克隆参考样本的"预设"：同一个说话人可以用不同的取材方式做出不同的克隆音色，让用户
自己听、自己选。

没有一种取材对所有说话人都最好（同一个人换一批句子，音色和口音泄漏的程度就变），
所以这里不追求一个最优规则，而是把几种实测过的取材方式做成可枚举的预设：

  continuous  平稳连读   原片里连续说的一段，保留自然停顿，不拼接
  long_one    单句长段   最完整的一句话
  mid_concat  中句拼接   几句 3~8 秒的平稳陈述句，句间插 0.5 秒底噪
  smart       智能挑选   omni 听着挑（由调用方处理，这里只声明它存在）

这个模块是纯函数，不依赖 dashscope / ffmpeg：视图层每次推送状态都会调它，必须便宜
且不会因为缺依赖而导入失败。素材量、可用预设都是**现算**的，不落盘。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.models import Segment

# 官方文档：推荐 10~20 秒，硬上限 60 秒；单次停顿 ≤ 2 秒。
SAMPLE_MAX_DURATION = 20.0
GAP_DURATION = 0.5          # 拼接时句间的间隔（实测 0.5 秒比 1 秒自然）
FADE_DURATION = 0.02        # 每段首尾淡入淡出，消除拼接处的咔哒声
RUN_GAP = 1.2               # 同一说话人相邻两句间隔不超过它，算"连续说"
MIN_SPEECH = 0.3            # 更短的碎片（"嗯"、咳嗽）不可能是连续清晰的朗读

MID_MIN, MID_MAX, MID_TARGET = 3.0, 8.0, 5.5
LONG_MAX = 12.0
CONT_SCORE_CAP = 15.0

PRESET_LABELS = {
    "continuous": "平稳连读",
    "long_one": "单句长段",
    "mid_concat": "中句拼接",
    "smart": "智能挑选",
}
PRESET_HINTS = {
    "continuous": "原片里连续说的一段，保留自然停顿，没有拼接痕迹",
    "long_one": "挑最完整的一句，口音和语调最统一",
    "mid_concat": "几句中等长度的平稳陈述句，句间 0.5 秒底噪",
    "smart": "让 AI 听着挑：避开疑问句、强调、口头禅，并考虑目标语言",
}
ALL_PRESETS = tuple(PRESET_LABELS)


class PresetUnavailable(Exception):
    """这个预设在这个说话人身上做不出来；消息是给人看的原因。"""


@dataclass
class Plan:
    preset: str                              # 预设 id；"manual" = 用户/AI 指定的行
    clips: list[tuple[float, float]]         # 要截取的时间段（秒），按时间顺序
    line_ids: list[str]                      # 涉及的分段 id（展示用）
    gap: float = GAP_DURATION                # 段间间隔；连读只有一段，用不到

    @property
    def seconds(self) -> float:
        voice = sum(b - a for a, b in self.clips)
        return voice + max(0, len(self.clips) - 1) * self.gap


# ----------------------------------------------------------------------------
# 候选池
# ----------------------------------------------------------------------------

def _dur(s: Segment) -> float:
    return s.end_time - s.start_time


def pool_for(segments: list[Segment], speaker_id: str) -> list[Segment]:
    """
    该说话人可作参考的行：语音、够长、**不与别的说话人重叠**。

    重叠检测是唯一的硬规则：diarization 的段里常混着别人插话，只要与任何其他说话人
    的时间范围重叠（0.05 秒容差）就淘汰，否则参考里的音色会被第二个人污染。情绪 /
    语速之类的文本启发式过滤已被明确移除（误伤正常句子），那类判断交给 omni 或用户。
    """
    speech = [s for s in segments if s.kind == "speech" and _dur(s) >= MIN_SPEECH]
    others = [(s.start_time, s.end_time) for s in speech if s.speaker_id != speaker_id]
    eps = 0.05
    mine = sorted((s for s in speech if s.speaker_id == speaker_id), key=lambda s: s.start_time)
    return [
        s for s in mine
        if not any(o0 < s.end_time - eps and o1 > s.start_time + eps for o0, o1 in others)
    ]


def _runs(segments: list[Segment], speaker_id: str, pool: list[Segment]) -> list[list[Segment]]:
    """原片里"连续说"的片段：同一说话人、相邻间隔 ≤ RUN_GAP、中间没有别人的话。"""
    pool_ids = {s.id for s in pool}
    ordered = sorted((s for s in segments if s.kind == "speech"), key=lambda s: s.start_time)
    runs: list[list[Segment]] = []
    cur: list[Segment] = []
    for s in ordered:
        mine = s.id in pool_ids and s.speaker_id == speaker_id
        if mine and cur and s.start_time - cur[-1].end_time <= RUN_GAP:
            cur.append(s)
            continue
        if cur:
            runs.append(cur)
        cur = [s] if mine else []
    if cur:
        runs.append(cur)
    return runs


# ----------------------------------------------------------------------------
# 素材量（现算，不存）
# ----------------------------------------------------------------------------

def material(segments: list[Segment], speaker_id: str) -> dict:
    """{"total": 可用语音总秒数, "longest": 最长连续段秒数, "lines": 候选句数}"""
    pool = pool_for(segments, speaker_id)
    runs = _runs(segments, speaker_id, pool)
    longest = max((r[-1].end_time - r[0].start_time for r in runs), default=0.0)
    return {
        "total": round(sum(_dur(s) for s in pool), 1),
        "longest": round(longest, 1),
        "lines": len(pool),
    }


# ----------------------------------------------------------------------------
# 预设
# ----------------------------------------------------------------------------

def plan_continuous(segments: list[Segment], speaker_id: str) -> Plan:
    pool = pool_for(segments, speaker_id)
    if not pool:
        raise PresetUnavailable("这个说话人没有可用的干净语音")
    best: Optional[tuple[float, float, list[Segment], tuple[float, float]]] = None
    for run in _runs(segments, speaker_id, pool):
        for i in range(len(run)):
            j = i
            while j + 1 < len(run) and run[j + 1].end_time - run[i].start_time <= SAMPLE_MAX_DURATION:
                j += 1
            window = run[i:j + 1]
            a, b = window[0].start_time, window[-1].end_time
            if b - a > SAMPLE_MAX_DURATION:          # 单句本身超长：只取开头
                b = a + SAMPLE_MAX_DURATION
            score = min(b - a, CONT_SCORE_CAP)
            key = (score, -a)
            if best is None or key > (best[0], best[1]):
                best = (score, -a, window, (a, b))
    assert best is not None
    return Plan("continuous", [best[3]], [s.id for s in best[2]])


def plan_long_one(segments: list[Segment], speaker_id: str) -> Plan:
    pool = pool_for(segments, speaker_id)
    if not pool:
        raise PresetUnavailable("这个说话人没有可用的干净语音")
    ideal = [s for s in pool if 5.0 <= _dur(s) <= LONG_MAX]
    if ideal:
        s = max(ideal, key=_dur)
        return Plan("long_one", [(s.start_time, s.end_time)], [s.id])
    s = max(pool, key=_dur)
    end = min(s.end_time, s.start_time + LONG_MAX)
    return Plan("long_one", [(s.start_time, end)], [s.id])


def plan_mid_concat(segments: list[Segment], speaker_id: str) -> Plan:
    pool = pool_for(segments, speaker_id)
    mids = [s for s in pool if MID_MIN <= _dur(s) <= MID_MAX]
    if len(mids) < 2:
        raise PresetUnavailable(
            f"没有足够的 {MID_MIN:.0f}~{MID_MAX:.0f} 秒中等长度句（只有 {len(mids)} 句）"
        )

    def penalty(s: Segment) -> float:
        # 句末上扬 / 感叹的句子带着更重的语调起伏，更容易把原语言的韵律泄漏进音色
        tail = (s.text or "").strip()[-1:]
        return abs(_dur(s) - MID_TARGET) + (3.0 if tail in "?？!！" else 0.0)

    chosen: list[Segment] = []
    total = 0.0
    for s in sorted(mids, key=penalty):
        add = _dur(s) + (GAP_DURATION if chosen else 0.0)
        if total + add > SAMPLE_MAX_DURATION:
            continue
        chosen.append(s)
        total += add
    if len(chosen) < 2:
        raise PresetUnavailable("中等长度句凑不出两句以上的样本")
    chosen.sort(key=lambda s: s.start_time)
    return Plan("mid_concat", [(s.start_time, s.end_time) for s in chosen], [s.id for s in chosen])


def plan_from_segments(preset: str, picked: list[Segment], gap: float = GAP_DURATION) -> Plan:
    """用户 / AI / omni 指定的行 → 计划（按时间顺序）。"""
    picked = sorted(picked, key=lambda s: s.start_time)
    return Plan(preset, [(s.start_time, s.end_time) for s in picked], [s.id for s in picked], gap)


_BUILDERS = {
    "continuous": plan_continuous,
    "long_one": plan_long_one,
    "mid_concat": plan_mid_concat,
}


def build_plan(preset: str, segments: list[Segment], speaker_id: str) -> Plan:
    """规则型预设。`smart` 需要 omni，由调用方处理。"""
    if preset not in _BUILDERS:
        raise PresetUnavailable(f"预设 {preset!r} 不是规则型预设")
    return _BUILDERS[preset](segments, speaker_id)


def default_plan(segments: list[Segment], speaker_id: str) -> Plan:
    """
    初始化时不问用户就得选一个：按素材量自适应，素材够就中句拼接，不够就退到连读 /
    单句，**不硬凑**。（旧版是"最长优先 + 素材不足就循环重复"：长句带更多原语言的语调，
    循环重复只会放大那几句的韵律。）
    """
    for preset in ("mid_concat", "continuous", "long_one"):
        try:
            return _BUILDERS[preset](segments, speaker_id)
        except PresetUnavailable:
            continue
    raise PresetUnavailable(f"说话人 {speaker_id} 没有可用于克隆的素材")


# ----------------------------------------------------------------------------
# 给界面看的清单
# ----------------------------------------------------------------------------

def preset_options(segments: list[Segment], speaker_id: str) -> list[dict]:
    """每个预设现在能不能做、为什么不能、会用到第几句。全部现算。"""
    index = {s.id: i + 1 for i, s in enumerate(segments)}
    has_pool = bool(pool_for(segments, speaker_id))
    out: list[dict] = []
    for preset in ALL_PRESETS:
        item: dict = {"id": preset, "label": PRESET_LABELS[preset], "hint": PRESET_HINTS[preset],
                      "available": True, "reason": None, "seconds": None, "lines": []}
        if preset == "smart":
            if not has_pool:
                item.update(available=False, reason="这个说话人没有可用的干净语音")
        else:
            try:
                plan = build_plan(preset, segments, speaker_id)
                item.update(seconds=round(plan.seconds, 1), lines=[index[i] for i in plan.line_ids if i in index])
            except PresetUnavailable as e:
                item.update(available=False, reason=str(e))
        out.append(item)
    return out


# ----------------------------------------------------------------------------
# 试听：固定的一句话
# ----------------------------------------------------------------------------
#
# 所有音色（当前的和候选）都念**同一句固定的话**，对比才公平；而且这句话不依赖工程里有没有
# 译文、译文是什么，换了工程也能横向比较。
#
# 这句话是照着"最容易听出口音 / 语调泄漏"来写的：
#   · 平静的陈述句，没有疑问、感叹、强调 —— 听到的是音色，不是表演；
#   · 7~9 秒，足够听出语速和节奏，又不会让人听到不耐烦；
#   · 带一个数字、两个分句和一个转折，能听出换气、停顿和句内语调的走向；
#   · 没有专有名词、品牌和外来词 —— 那些词的读法本来就因语言而异，会干扰判断。
# 十种目标语言各一句，意思相同。

PREVIEW_SENTENCES = {
    "Chinese": "今天上午我去了一趟图书馆，借了三本书，其中一本讲的是这座城市的历史，我打算周末好好读一读。",
    "English": "This morning I went to the library and borrowed three books. One of them is about the history of this city, and I plan to read it properly this weekend.",
    "Japanese": "今朝、図書館に行って本を三冊借りました。そのうちの一冊はこの街の歴史についての本で、週末にじっくり読むつもりです。",
    "Korean": "오늘 아침에 도서관에 가서 책을 세 권 빌렸습니다. 그중 한 권은 이 도시의 역사에 관한 책인데, 주말에 차분히 읽어 볼 생각입니다.",
    "French": "Ce matin, je suis allé à la bibliothèque et j'ai emprunté trois livres. L'un d'eux parle de l'histoire de cette ville, et je compte le lire tranquillement ce week-end.",
    "German": "Heute Morgen war ich in der Bibliothek und habe mir drei Bücher ausgeliehen. Eines davon handelt von der Geschichte dieser Stadt, und ich möchte es am Wochenende in Ruhe lesen.",
    "Portuguese": "Hoje de manhã fui à biblioteca e emprestei três livros. Um deles fala da história desta cidade, e pretendo lê-lo com calma no fim de semana.",
    "Italian": "Stamattina sono andato in biblioteca e ho preso in prestito tre libri. Uno di essi parla della storia di questa città, e penso di leggerlo con calma durante il fine settimana.",
    "Vietnamese": "Sáng nay tôi đã đến thư viện và mượn ba cuốn sách. Một trong số đó nói về lịch sử của thành phố này, và tôi định đọc thật kỹ vào cuối tuần.",
    "Indonesian": "Pagi ini saya pergi ke perpustakaan dan meminjam tiga buku. Salah satunya tentang sejarah kota ini, dan saya berencana membacanya dengan tenang di akhir pekan.",
}


def preview_sentence(language: Optional[str]) -> str:
    """目标语言对应的固定试听句；没有就退到英文（不会因为语言没收录而试听不了）。"""
    return PREVIEW_SENTENCES.get(language or "", PREVIEW_SENTENCES["English"])


def preview_filename(voice_id: str, text: str) -> str:
    """
    试听文件名：音色 + 句子内容。同一个音色同一句话只合成一次（不重复花钱）；以后改了
    试听句，文件名跟着变，旧的自然失效。
    """
    import hashlib
    h = hashlib.sha1(f"{voice_id}|{text}".encode()).hexdigest()[:12]
    return f"pv_{h}.mp3"
