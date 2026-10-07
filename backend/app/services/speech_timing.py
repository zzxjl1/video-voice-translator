"""
文本框与时长/语速的换算。

从 `llm_service` 里搬出来的：这套数学（按语言数字符/音节、按经验速度估时长、
反推语速）和"调模型翻译"没有关系，而它自己是一组自洽的常量与公式；混在一起
让那个 900 行的翻译模块又长了一段，也让想调时长模型的人得先在翻译代码里找。

速度常量仍然来自 `config`（`speech_units_per_second`），TTS 的实际节拍以那边
为准 —— 这里只是复用它做预测。
"""

import re

from app import config


def text_length(text: str, language: str) -> int:
    """
    'Speech length' in the unit the LLM reasons in: characters for CJK, words
    for latin scripts. This is what `maxLength` in the prompt is expressed in.

    For estimating how long a line will TAKE, use `speech_units` instead — a
    character count is a poor proxy for spoken duration.
    """
    if language in ("Chinese", "Japanese", "Korean"):
        # Count non-whitespace characters.
        return len(re.sub(r"\s+", "", text))
    return len(text.split())


_SYLLABLE_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
_DIGIT_RUN_RE = re.compile(r"\d+")


def speech_units(text: str) -> float:
    """
    Estimate how many spoken units (roughly syllables) a text contains.

    Duration scales with this, not with the character count, and the two can
    differ by several times:

        Python   6 characters -> ~2 units  (read "pai-sen")
        API      3 characters ->  3 units  (spelled out letter by letter)
        73%      3 characters -> ~8 units  ("qi-shi-san" + "percent")
        ……       2 characters ->  3 units  (a pause; it makes no sound)

    Measured against real synthesis (leave-one-out, 6 samples): predicting
    duration from a character count was 21% off on average; this estimate gets
    that down to 11%. See `config.SPEECH_UNITS_PER_SECOND` for the rate and
    `config.TTS_FIXED_OVERHEAD` for the per-line fixed cost.
    """
    units = float(len(_SYLLABLE_RE.findall(text)))

    for word in _LATIN_WORD_RE.findall(text):
        # Short ALL-CAPS tokens are read letter by letter; ordinary words are
        # read as one syllable, or a couple for longer ones.
        units += len(word) if (word.isupper() and len(word) <= 5) else 1.5

    # Digits are read one at a time.
    units += sum(len(run) for run in _DIGIT_RUN_RE.findall(text))

    # An ellipsis or dash is a pause: no sound, but it costs time.
    units += (text.count("…") + text.count("——")) * 1.5

    return units


def predict_duration(text: str, language: str) -> float:
    """
    Best-effort spoken duration for `text`, in seconds.

    Linear rather than proportional for a reason: a line costs a fixed amount of
    time regardless of length (measured: a 3-character Chinese line still takes
    0.78s), so a pure ratio badly under-predicts short lines.
    """
    return (
        speech_units(text) / config.speech_units_per_second(language)
        + config.TTS_FIXED_OVERHEAD
    )


def plan_speech_rate(text: str, language: str, target_duration: float) -> float:
    """
    The `speech_rate` that should make `text` fill `target_duration`.

    This is a prediction and it is not precise (measured ~11% average error),
    but it is free — it does not add a TTS call — and it decouples the residual
    from how badly the translation overshot its slot.
    """
    if target_duration <= 0:
        return 1.0
    rate = predict_duration(text, language) / target_duration
    return max(config.TTS_RATE_MIN, min(config.TTS_RATE_MAX, rate))
