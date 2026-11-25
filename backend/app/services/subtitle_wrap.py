"""
字幕断行：在断点处切，两行长度平衡，绝不在词中间断。

专业字幕的断行规则（Netflix TTSG、BBC、Aegisub 的惯例）：
  1. 优先在**标点之后**断（句号、逗号、冒号、分号），其次在连词/介词之前
  2. 两行长度要**平衡**（金字塔），不是第一行塞满、第二行只剩一两个词
  3. 行首不能是收尾标点（。，！？）或收尾括号；行尾不能是开头标点
  4. 拉丁文绝不在词中间断；CJK 可以逐字断，但不在数字/专有名词中间断
  5. 两行都不得超过 max 格

用动态规划：把文本切成若干"原子"（拉丁词 / 单个 CJK 字符 / 标点），在原子边界
处枚举所有断法，取代价最小的。代价 = 每行"离 max 太远"的惩罚 + 不平衡惩罚 +
坏断点惩罚。文本一般不超过一两百字符，O(n²) 完全够。
"""
from __future__ import annotations

import re
from functools import lru_cache

# 行首不允许出现的字符（收尾标点、收尾括号）
_NO_LINE_START = set("，。！？；：、）》」』】〉”’…,.;:!?)]}")
# 行尾不允许出现的字符（开头标点）
_NO_LINE_END = set("（《「『【〈“‘([{")
# 高质量断点：在这些之后断最自然（句末 > 从句 > 短暂停顿）
_BREAK_AFTER_STRONG = set("。！？；…!?;")
# 括号类：内部不断（《创：战纪》里的冒号不是断点）
# 括号类：内部不断（《创：战纪》里的冒号不是断点）。只收**成对**的中英文括号/引号；
# 英文撇号（crane's）不成对，不能算——否则 depth 永远回不到 0，整段断不开。
_OPEN_BRACKETS = set("《「『【〈（(“‘")
_CLOSE_BRACKETS = set("》」』】〉）)”’")
_BREAK_AFTER_WEAK = set("，、：,:")
# 连词/介词：在这些**之前**断（英文），避免把 "of the" 拆到两行
_BREAK_BEFORE_EN = {
    "and", "or", "but", "so", "because", "if", "when", "while", "that", "which",
    "of", "to", "in", "on", "at", "for", "with", "from", "by", "as", "than",
}
# 中文连词/介词：在这些**之前**断
_BREAK_BEFORE_ZH = {"但是", "因为", "所以", "如果", "而且", "然后", "不过", "还是", "或者"}
# 中文：这些字不能出现在行首（必须紧跟前一个字/词）
# 只禁真正的"后缀虚词"（必须紧跟前面的动词/名词）；副词/连词（就、也、而、但、或、在、把、被）可以行首
_ZH_NO_LINE_START = set("了的着过吗呢吧啊呀嘛咧啦")
# 中文：这些字后面断最自然（句读）
_ZH_BREAK_AFTER = set("，。！？；：、）」』】”’…")
# 中文：这些字后面尽量不断（动词+补语、数词+量词）
_ZH_NO_BREAK_AFTER = set("一二三四五六七八九十第")
# 中文代词+副词常见搭配不断（你这 那你 我也 他都 …）——用轻惩罚而非禁止
_ZH_WEAK_AFTER = set("你我他她它这那哪谁")


def _display_width(text: str) -> float:
    """
    显示宽度（"拉丁字符"为单位）：拉丁/半角 1，CJK/全角 2。
    与 Netflix TTSG 一致（CJK 字符占拉丁的两倍宽度）。
    `max_chars` 的语义是**拉丁字符数**：42 → 拉丁 42 字符，中文 21 个字。
    """
    if text.isascii():
        return float(len(text))          # ASCII 没有宽字符/组合标记，C 速取长度
    return sum(0.0 if _COMBINING_RE.match(ch) else (2.0 if _is_wide(ch) else 1.0) for ch in text)


# 组合标记（元音符号、声调、鼻音化…）：不占显示宽度，且必须和前面的基字符留在同一行
_COMBINING_RE = re.compile(r"[\u0300-\u036F\u0591-\u05BD\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06DC"
                           r"\u0900-\u0903\u093A-\u094F\u0951-\u0957\u0962-\u0963\u0981-\u0983\u09BC-\u09CD"
                           r"\u0A01-\u0A03\u0A3C-\u0A51\u0A81-\u0A83\u0ABC-\u0ACD\u0B01-\u0B03\u0B3C-\u0B4D"
                           r"\u0C00-\u0C04\u0C3E-\u0C56\u0CBF-\u0CD6\u0D41-\u0D44\u0DCA\u0E31\u0E34-\u0E3A"
                           r"\u0E47-\u0E4E\u0EB1\u0EB4-\u0EBC\u0F71-\u0F84\u102B-\u103E\u1712-\u1714"
                           r"\u17B4-\u17D3\u1A17-\u1A1B\u1AB0-\u1AFF\u1DC0-\u1DFF\u20D0-\u20F0]")
# 逐字书写（无空格）的字母文字：泰/老挝/高棉/缅甸/柬埔寨——按"基字符 + 组合符号"为一个音节单位
_SYLLABIC_RANGES = ((0x0E00, 0x0E7F), (0x0E80, 0x0EFF), (0x1000, 0x109F), (0x1000, 0x109F), (0xA000, 0xA48F))


def _is_wide(ch: str) -> bool:
    """占一个 CJK 格（= 2 个拉丁字符宽）的字符：CJK、日文假名、韩文、泰/老挝/高棉/缅甸。"""
    code = ord(ch)
    return (
        0x1100 <= code <= 0x115F
        or 0x2E80 <= code <= 0xA4CF
        or 0xAC00 <= code <= 0xD7A3
        or 0xF900 <= code <= 0xFAFF
        or 0xFE30 <= code <= 0xFE6F
        or 0xFF00 <= code <= 0xFF60
        or 0xFFE0 <= code <= 0xFFE6
        or 0x20000 <= code <= 0x3FFFD
        or 0x3040 <= code <= 0x30FF          # 平假名 / 片假名
        or 0x0E00 <= code <= 0x0E7F          # 泰语
        or 0x0E80 <= code <= 0x0EFF          # 老挝
        or 0x1000 <= code <= 0x109F          # 高棉
        or 0xA000 <= code <= 0xA48F          # 彝文（少见，顺带）
    )


# 一个"原子"：一个"词"（任意 Unicode 字母/数字，含内部 ' ’ - ）、一段连续空格、单个其它字符。
# 必须是 \w 而不是 [A-Za-z]：越南语（ô ả ụ）、西里尔、希腊、天城文、阿拉伯文都是字母，
# 用 ASCII 类会把它们当成逐字符切（khô|ng、м|огу）。CJK 不在此列：\w 虽然匹配汉字，
# 但 CJK 要逐字成原子（再由 jieba 合并成词），所以单独排除——否则一个汉字串会变成一个原子。
_ATOM_RE = re.compile(
    r"(?:(?![\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3\uf900-\ufaff])[\w'’\-])+|\s+|.",
    re.UNICODE,
)

_jieba_ready = False


_zh_pos_cache: dict[str, list[tuple[str, str]]] = {}
# 自定义词典（人名/地名/剧名——jieba 不认识的生造词，比如"斯普利特河"）。
# 由 build_cues 从术语表喂进来；同一进程只注册一次。
_custom_words: set[str] = set()


def add_custom_words(words) -> None:
    """登记专有名词，避免断行把它们拆开（"斯普利特|河高中"）。幂等。"""
    global _jieba_ready
    new = {str(w).strip() for w in words if w and len(str(w).strip()) >= 2}
    new -= _custom_words
    if not new:
        return
    _custom_words.update(new)
    try:
        import jieba  # type: ignore
        if not _jieba_ready:
            jieba.setLogLevel(60)
            jieba.initialize()
            _jieba_ready = True
        for w in new:
            jieba.add_word(w, freq=1_000_000, tag="n")   # 高频 + 名词：整词成原子、词性为 n
        _zh_pos_cache.clear()   # 词典变了，缓存失效
    except ImportError:
        pass


def _zh_cut(text: str) -> list[tuple[str, str]]:
    """
    中文分词 + 词性（jieba.posseg），返回 [(词, 词性)]；非中文文本返回 []。
    按需初始化（0.25s，41MB 词典）——只在真正有 CJK 文本要断行时才加载。
    jieba 分错的词没有严重影响：它只是用来**惩罚**在词中间断，不是禁止。
    """
    global _jieba_ready
    if text.isascii() or not any(_is_wide(c) for c in text):
        return []
    hit = _zh_pos_cache.get(text)
    if hit is not None:
        return hit
    try:
        import jieba  # type: ignore
        import jieba.posseg as pseg  # type: ignore
        if not _jieba_ready:
            jieba.setLogLevel(60)
            jieba.initialize()
            _jieba_ready = True
        cut = [(w, f) for w, f in pseg.lcut(text)]
    except ImportError:
        cut = [(ch, "") for ch in text]
    if len(_zh_pos_cache) > 4096:
        _zh_pos_cache.clear()
    _zh_pos_cache[text] = cut
    return cut


def _zh_words(text: str) -> set[str]:
    """文本里出现过的多字中文词（用来避免在词中间断）。"""
    return {w for w, _ in _zh_cut(text) if len(w) >= 2 and not _is_punct(w)}


# 词性分组（jieba posseg 标记）
_POS_NOUN = set("n nr ns nt nz s nrt nw an".split())   # 名词：人名/地名/处所/工作/方位…
_POS_PRON = {"r"}                              # 代词
_POS_TIME = set("t tg".split())               # 时间词（正在、刚刚、今天）
_POS_ADJ = set("a ad an b".split())            # 形容词/区别词（作定语时）
_POS_ADV = set("d")                            # 副词
_PREP = set("p")                               # 介词
_POS_VERB = set("v vn vd vg")                  # 动词
_PARTICLE_DE = {"uj"}                          # 的
_POS_FUNC = set("c u ul y x e o".split())      # 连词/助词/语气/标点


def _zh_boundary_cost(prev_word: str, prev_pos: str, next_word: str, next_pos: str) -> float | None:
    """
    中文词性边界代价（None = 不适用，交给通用规则）。对照专业字幕的断点习惯：
      · 名词/代词 后、副词/时间词/介词/动词 前 → 主谓边界，**最好**的断点
      · 动词 后、名词 前 → 述宾边界，次好
      · 「的」后：前面是动词（找到另一半的）可断；前面是名词（乐队的排练）不断
      · 助词/量词 后不断（了、着、个）
    """
    if prev_pos in _POS_FUNC or next_pos in _POS_FUNC and next_pos not in {"c"}:
        return None
    if prev_pos in (_POS_NOUN | _POS_PRON) and (next_pos in _POS_TIME or next_pos in _POS_ADV or next_pos in _PREP):
        return 0.0          # 主谓
    if prev_pos in (_POS_NOUN | _POS_PRON) and next_pos in _POS_VERB:
        return 0.1          # 主谓（名词+动词）
    if prev_pos in _POS_VERB and next_pos in _POS_NOUN:
        return 0.25         # 述宾
    if prev_pos in _PARTICLE_DE:
        return 0.3          # 的（后面再按前面是不是名词细化——见调用处）
    if prev_pos in _POS_VERB and next_pos in (_POS_PRON | _POS_NOUN):
        return 0.3
    if prev_pos in _POS_ADV and next_pos in _POS_VERB:
        return 0.5          # 副词+动词：可以断但不优先
    if prev_pos in _POS_NOUN and next_pos in _PARTICLE_DE:
        return 0.6          # 名词+的：定语还没完
    if prev_pos in _POS_VERB and next_pos in _POS_VERB:
        return 0.7          # 连动
    return None


def _atoms(text: str) -> list[str]:
    atoms = _ATOM_RE.findall(text)
    # 组合标记并入前一个原子：天城文/泰语的元音符号和声调必须跟着基字符（म + ै = मै）。
    # ASCII 文本没有组合标记，跳过整个循环（预览热路径，每次拖滑块都跑）。
    if not text.isascii():
        merged: list[str] = []
        for a in atoms:
            if merged and a and _COMBINING_RE.match(a[0]):
                merged[-1] += a
            else:
                merged.append(a)
        atoms = merged
    # 中文：把 jieba 切出的多字词合并成一个原子，断行就不会落在词中间
    words = _zh_words(text)
    if not words:
        return atoms
    joined = "".join(atoms)
    out: list[str] = []
    i = 0
    n = len(joined)
    while i < n:
        matched = None
        for w in sorted(words, key=len, reverse=True):
            if joined.startswith(w, i):
                matched = w
                break
        if matched:
            out.append(matched)
            i += len(matched)
        else:
            out.append(joined[i])
            i += 1
    return out


def _is_punct(atom: str) -> bool:
    return bool(atom) and not atom.isspace() and not any(c.isalnum() for c in atom)


def _atom_break_cost(atoms: list[str], i: int) -> float:
    """
    在 atoms[i] 之前断的代价（0 = 完美断点）。i == 0 或 i == len 不可断。
    规则见模块 docstring。
    """
    if i <= 0 or i >= len(atoms):
        return float("inf")
    prev = atoms[i - 1]
    nxt = atoms[i]
    # 跳过空白：看空白两侧的实际字符
    j = i - 1
    while j > 0 and atoms[j].isspace():
        j -= 1
    prev = atoms[j]
    k = i
    while k < len(atoms) and atoms[k].isspace():
        k += 1
    if k >= len(atoms):
        return float("inf")
    nxt = atoms[k]

    # 硬规则：行首不能是收尾标点，行尾不能是开头标点
    if nxt and nxt[0] in _NO_LINE_START:
        return float("inf")
    if prev and prev[-1] in _NO_LINE_END:
        return float("inf")
    # 书名号/引号/括号内部不断：《创：战纪》里的冒号不是断点
    depth = 0
    for a in atoms[:j + 1]:
        for ch in a:
            if ch in _OPEN_BRACKETS:
                depth += 1
            elif ch in _CLOSE_BRACKETS:
                depth -= 1
    if depth > 0:
        return float("inf")
    # 中文硬规则：虚词不能行首，数词/第 后面不断
    if nxt and nxt[0] in _ZH_NO_LINE_START:
        return float("inf")
    if prev and prev[-1] in _ZH_NO_BREAK_AFTER:
        return float("inf")

    # 词内部不断（原子已经保证），但数字和后面的单位/符号不断开：如 "3.14" "%"
    if prev and nxt and prev[-1].isdigit() and nxt[0] in "%‰°":
        return float("inf")

    # 中文：有词性信息时，用词性边界代价（覆盖默认的逐字代价）
    zh_cost = None
    if prev and nxt and _is_wide(prev[-1]) and _is_wide(nxt[0]) and len(atoms) > 2:
        cut = _zh_cut("".join(atoms))
        # 找 prev 这个原子在 cut 里对应的词性：按字符位置对齐
        left_text = "".join(atoms[:j + 1])
        pos_i = 0
        pw = pf = nw = nf = None
        for w, f in cut:
            if left_text.endswith(w):
                pw, pf = w, f
                break
            pos_i += len(w)
        # 下一个原子开头的词
        right_text = "".join(atoms[k:])
        for w, f in cut:
            if right_text.startswith(w):
                nw, nf = w, f
                break
        if pw and nw:
            zh_cost = _zh_boundary_cost(pw, pf or "", nw, nf or "")
    cost = 0.0
    # 标点后是最强的断点信号（专业字幕 32% 断在 ，。？后），要明显优于词性边界
    if prev and prev[-1] in _ZH_BREAK_AFTER:
        cost += 0.0
    elif zh_cost is not None:
        cost += zh_cost
    elif prev and prev[-1] in _ZH_WEAK_AFTER and nxt and _is_wide(nxt[0]):
        cost += 0.8
    elif _is_punct(prev):
        if prev[-1] in _BREAK_AFTER_STRONG:
            cost += 0.0
        elif prev[-1] in _BREAK_AFTER_WEAK:
            cost += 0.3
        else:
            cost += 1.5
    elif nxt and _is_punct(nxt):
        # 标点前不断（把标点留到下一行开头是错的，上面已禁止；这里处理"标点紧跟前一个词"）
        cost += 2.0
    elif atoms[i - 1].isspace() or atoms[i].isspace():
        # 用空格分词的语言（拉丁/西里尔/希腊/天城文/阿拉伯…）：词间断永远优于词内，
        # 和具体语言无关。连词/介词之前断更好（英文有词表；其它语言退化为普通词间）
        word_low = nxt.lower().strip()
        if word_low in _BREAK_BEFORE_EN:
            cost += 0.2
        else:
            cost += 0.5
    else:
        # 词与词之间（无空格的语言）：连词之前断好
        if nxt in _BREAK_BEFORE_ZH:
            cost += 0.2
        elif _is_wide(nxt[0]) and len(nxt) == 1:
            # CJK 逐字断：默认可接受
            cost += 0.6
        else:
            cost += 1.0
    return cost


def _line_cost(line: str, max_chars: float) -> float:
    """一行"多合适"：超宽重罚，离 max 太远轻罚（鼓励用足宽度，减少行数）。"""
    w = _display_width(line)
    if w > max_chars + 1e-9:
        return float("inf")
    # 离满行的距离：二次惩罚，但权重**低于**断点代价——专业字幕宁可在标点后断、
    # 第二行短一点，也不为了填满宽度断在语法中间（"我很高兴你找到了另一半，| 真的，"）。
    # 中文尤其如此：拉丁文宽度权重 1.0，中文 0.5（词性边界更可靠）。
    shortfall = max_chars - w
    weight = 0.25 if any(_is_wide(c) for c in line) else 1.0
    return (shortfall / max(max_chars, 1.0)) ** 2 * weight


@lru_cache(maxsize=512)
def _best_breaks(text: str, max_chars: float) -> tuple[int, ...]:
    """返回每行起始原子下标（含 0）。用 DP：cost[i] = 放第 i 个原子开头的行的最小总代价。"""
    atoms = _atoms(text)
    n = len(atoms)
    if n == 0:
        return ()
    # 前缀宽度（含空格）
    prefix = [0.0]
    for a in atoms:
        prefix.append(prefix[-1] + _display_width(a))

    INF = float("inf")
    best = [INF] * (n + 1)
    best[0] = 0.0
    back = [-1] * (n + 1)
    for i in range(n):
        if best[i] == INF:
            continue
        for j in range(i + 1, n + 1):
            line = "".join(atoms[i:j]).strip()
            if not line:
                continue
            w = _display_width(line)
            if w > max_chars:
                break
            lc = _line_cost(line, max_chars)
            # 行数惩罚：每多一行固定加一点，避免为了"平衡"把短句切太碎
            step = lc + 0.4
            if j < n:
                step += _atom_break_cost(atoms, j)
                # "跨过强断点"惩罚：这一行里有一个强断点（，。？！：）后面还有 ≥2 个原子，
                # 却没在那断——专业字幕几乎总在标点后断（语料：第一行占 28% 也在逗号后断）。
                # 按跨过的强断点数累加；行很短（< 35% cap）时不罚，短句开头例外。
                # 只罚"跨过了标点、而且行已经不短"：一句话常有 2–3 个逗号，不能每个都断。
                # 55% 是语料里"专业字幕选择跨过逗号"的典型上限，再长就该在标点后断。
                if _display_width(line) >= max_chars * 0.55:
                    skipped = 0
                    for k in range(i + 1, j):
                        a_prev = "".join(atoms[:k]).rstrip()[-1:]
                        if a_prev in "，。？！：,?!:":
                            skipped += 1
                    step += skipped * 0.5
            if best[i] + step < best[j]:
                best[j] = best[i] + step
                back[j] = i
    if best[n] == INF:
        # 没有合法断法（例如一个超长词）：退化为逐字符硬切
        return tuple(range(0, n, max(1, int(max_chars * 2))))
    starts = []
    j = n
    while j > 0:
        starts.append(back[j])
        j = back[j]
    return tuple(reversed(starts))


def _balance_pair(lines: list[str], max_chars: float) -> list[str]:
    """
    两行时做"金字塔"平衡：如果第一行比第二行长得多，尝试把断点往前挪。
    Netflix 风格是上短下长或等长，不是上长下短。
    """
    if len(lines) != 2:
        return lines
    a, b = lines
    wa, wb = _display_width(a), _display_width(b)
    if wa <= wb * 1.15 or wa <= max_chars * 0.5:
        return lines
    # 尝试找一个更好的断点：代价 = 不平衡度 + 断点代价（不能为了平衡在词中间断）
    text = f"{a} {b}"
    atoms = _atoms(text)
    base = abs(wa - wb) + 0.0
    best_i, best_score = None, base
    for i in range(1, len(atoms)):
        left = "".join(atoms[:i]).strip()
        right = "".join(atoms[i:]).strip()
        if not left or not right:
            continue
        lw, rw = _display_width(left), _display_width(right)
        if lw > max_chars or rw > max_chars:
            continue
        bc = _atom_break_cost(atoms, i)
        if bc == float("inf"):
            continue
        # 拉丁文每行至少 2 个词，避免 "why this is such / a big deal" 这种切法
        if not _is_wide(left[0]) and (left.count(" ") < 1 or right.count(" ") < 1):
            continue
        score = abs(lw - rw) + bc * 4.0
        if score < best_score - 0.5:
            best_score, best_i = score, i
    if best_i is None:
        return lines
    left = "".join(atoms[:best_i]).strip()
    right = "".join(atoms[best_i:]).strip()
    return [left, right]


def wrap_text(text: str, max_cells: float, *, balance: bool = True) -> list[str]:
    """
    把文本断成若干行，每行不超过 `max_cells` 个 **CJK 格**（1 格 = 1 个汉字 = 2 个拉丁字符）。

    `max_cells` 就是工程里存的 `max_chars_per_line`（历史上一直是这个语义：18 格 = 18 汉字
    = 36 拉丁字符）。内部按 Netflix TTSG 的宽度（拉丁 1、CJK 2）计算，所以换算成拉丁
    字符数是 max_cells × 2。
    拉丁文在词边界断，CJK 在词（jieba）边界断，标点规则见模块 docstring。
    `balance=True` 时对两行结果做金字塔平衡。
    """
    text = " ".join((text or "").split())
    if not text:
        return []
    max_chars = max_cells * 2.0
    if _display_width(text) <= max_chars:
        return [text]
    atoms = _atoms(text)
    starts = _best_breaks(text, float(max_chars))
    lines = []
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(atoms)
        line = "".join(atoms[s:e]).strip()
        if line:
            lines.append(line)
    # 中文不做"金字塔平衡"：专业字幕断在语法边界（主谓/述宾），不是按长度平衡；
    # _balance_pair 不懂词性，会把 DP 选好的断点挪坏。拉丁文仍平衡。
    if balance and not any(_is_wide(c) for c in text):
        lines = _balance_pair(lines, float(max_chars))
    return lines if lines else [text]
