"""
从项目文档**算出**每一行的状态 —— 取代旧版"看文件在不在"的猜测。

两个指纹，各管一件事：

  译文指纹  = hash(翻译基底文本, 目标语言, 自定义要求)
  配音指纹  = hash(译文, 音色, 语速, 语气指令, TTS 模型, 目标语言)

行上存着"当初产出这份译文/配音时的指纹"，这里算出"按现在的设置应该是什么指纹"，
两者对不上就是过期。于是：

  · 改原文 → 基底变了 → 译文过期；
  · 改目标语言 / 自定义要求 → 所有译文过期；
  · 改译文 / 换音色 / 调语速 / 改语气指令 / 换语言 → 对应的配音过期；
  · 撤销一次改动 → 指纹自然对回去，旧 mp3 重新"新鲜"，不用重新合成。

不再有"作废全部配音""恢复时扫描目录把旧 mp3 认回来"这类逻辑：一份 mp3 只有在指纹
对得上时才算数，过期的留在磁盘上也不会被用到（下次合成会覆盖它）。
"""
import hashlib
import os
from typing import Optional

from app import config
from app.models import Segment, Speaker, VideoState, tts_audio_path

# 配音状态 / 译文状态
NONE = "none"        # 这一行不需要（非语音、静音、没有原文…）
MISSING = "missing"  # 需要但还没有
STALE = "stale"      # 有，但是按旧设置做的
FRESH = "fresh"      # 有，且与当前设置一致

MAX_INSTRUCTION = 128   # SDK 文档：instruction 最长 128


def _h(*parts) -> str:
    return hashlib.sha1("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# 译文
# ----------------------------------------------------------------------------

def translation_source(state: VideoState, seg: Segment) -> str:
    """
    翻译用的基底：开着审听增强且有审听版就用它（纠错 + 语气标签），否则原文。

    关掉增强后，磁盘上留着的审听版不该再被套用 —— 用户明明在界面上关了它。
    """
    if state.mm_enhance and (seg.reviewed_text or "").strip():
        return seg.reviewed_text.strip()
    return (seg.text or "").strip()


def expected_tr_fp(state: VideoState, seg: Segment) -> str:
    return _h("tr", translation_source(state, seg), state.target_language, state.custom_prompt)


def tr_status(state: VideoState, seg: Segment) -> str:
    if seg.kind != "speech" or not translation_source(state, seg):
        return NONE
    if not (seg.translated_text or "").strip():
        return MISSING
    return FRESH if seg.tr_fp == expected_tr_fp(state, seg) else STALE


# ----------------------------------------------------------------------------
# 译文是不是目标语言（廉价的文字脚本检查）
# ----------------------------------------------------------------------------

# 目标语言 → 它用的文字脚本。只覆盖"脚本一眼就能分辨"的语言；拉丁字母语言之间
# （英/法/德/西…）靠脚本分不出来，这里不假装能分。
_SCRIPT = {
    "Chinese": "cjk", "Japanese": "cjk", "Korean": "hangul",
    "English": "latin", "French": "latin", "German": "latin", "Spanish": "latin",
    "Portuguese": "latin", "Italian": "latin", "Indonesian": "latin", "Vietnamese": "latin",
    "Russian": "cyrillic",
}


def _script_ratio(text: str) -> dict:
    """文本里各种文字脚本的字符占比（只统计字母，忽略标点/数字/空白/[标签]）。"""
    import re
    body = re.sub(r"\[[^\]]*\]", "", text)
    counts = {"cjk": 0, "hangul": 0, "latin": 0, "cyrillic": 0}
    for ch in body:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF:
            counts["cjk"] += 1                      # 汉字 + 假名
        elif 0xAC00 <= o <= 0xD7AF or 0x1100 <= o <= 0x11FF:
            counts["hangul"] += 1
        elif 0x0400 <= o <= 0x04FF:
            counts["cyrillic"] += 1
        elif ch.isalpha() and o < 0x250:
            counts["latin"] += 1
    total = sum(counts.values())
    return {k: (v / total if total else 0.0) for k, v in counts.items()}


def untranslated_suspect(state: VideoState, seg: Segment) -> bool:
    """
    这句"译文"看起来根本没被翻译（还是原文的文字脚本）。

    翻译模型偶尔会原样返回原文；用户或 AI 手写译文时也可能写错语言。指纹只证明译文是在
    当前设置下写入的，证明不了它**是**目标语言 —— 而这种错会一路配成音、进成片，
    只有听到才发现。这是个脚本级的粗检查：拉丁字母语言之间分不出来，所以只在能确定时
    才报（目标是英文而译文 90% 以上是汉字，这类）。
    """
    target = _SCRIPT.get(state.target_language)
    text = (seg.translated_text or "").strip()
    if not target or not text or seg.kind != "speech":
        return False
    ratio = _script_ratio(text)
    if sum(ratio.values()) == 0:
        return False
    # 译文的主要脚本和目标语言的脚本不同，且占了绝大多数
    dominant = max(ratio, key=ratio.get)
    return dominant != target and ratio[dominant] >= 0.9 and len(text) >= 2


# ----------------------------------------------------------------------------
# 配音
# ----------------------------------------------------------------------------

def clone_voice_id(state: VideoState, speaker: Optional[Speaker]) -> Optional[str]:
    """说话人的克隆音色 —— 仅当克隆开着、且是按**当前** TTS 模型登记的（克隆音色不能跨模型）。"""
    if not state.enable_voice_clone or speaker is None or not speaker.clone:
        return None
    if speaker.clone.get("target_model") != config.TTS_MODEL:
        return None
    return speaker.clone.get("voice_id") or None


def resolve_voice(state: VideoState, seg: Segment) -> Optional[str]:
    """优先级：本行指定 > 克隆音色 > 说话人的系统音色。都没有返回 None（合成时再分配）。"""
    if seg.tts_voice:
        return seg.tts_voice
    speaker = state.speaker(seg.speaker_id)
    return clone_voice_id(state, speaker) or (speaker.voice if speaker else None)


def dub_instruction(state: VideoState, seg: Segment) -> Optional[str]:
    """方言指令 + 本行语气指令，合成一条（SDK 限 128 字符）。"""
    parts = [
        config.tts_instruction(state.target_language, state.accent),
        (seg.tts_instruction or "").strip() or None,
    ]
    joined = " ".join(p for p in parts if p)
    return joined[:MAX_INSTRUCTION] or None


def dubbable(state: VideoState, seg: Segment) -> bool:
    """这一行该不该有配音。"""
    return (
        state.enable_dubbing
        and seg.kind == "speech"
        and not seg.muted
        and bool((seg.translated_text or "").strip())
    )


def expected_tts_fp(state: VideoState, seg: Segment) -> Optional[str]:
    voice = resolve_voice(state, seg)
    if not voice:
        return None
    return _h(
        "tts", (seg.translated_text or "").strip(), voice,
        seg.tts_rate if seg.tts_rate is not None else "",
        dub_instruction(state, seg) or "", config.TTS_MODEL, state.target_language,
    )


def tts_status(state: VideoState, seg: Segment) -> str:
    if not dubbable(state, seg):
        return NONE
    if not os.path.exists(tts_audio_path(state.video_id, seg.id)):
        return MISSING
    expected = expected_tts_fp(state, seg)
    # 音色解析不出来（比如换语言清掉了音色）：音频是用旧音色做的，按过期算。
    return FRESH if (expected is not None and seg.tts_fp == expected) else STALE


# ----------------------------------------------------------------------------
# 汇总
# ----------------------------------------------------------------------------

def hydrate(state: VideoState) -> VideoState:
    """给每行填上派生的 `audio_path`（仅配音新鲜时有）。就地修改并返回。"""
    for seg in state.segments:
        seg.audio_path = (
            tts_audio_path(state.video_id, seg.id)
            if tts_status(state, seg) == FRESH else None
        )
    return state


def line_statuses(state: VideoState) -> dict[str, tuple[str, str]]:
    """{segment_id: (译文状态, 配音状态)}"""
    return {s.id: (tr_status(state, s), tts_status(state, s)) for s in state.segments}


def needs(state: VideoState) -> dict:
    """还差多少活：要翻译（缺或过期）的行数、要配音（缺或过期）的行数。"""
    translate = dub = 0
    for seg in state.segments:
        if tr_status(state, seg) in (MISSING, STALE):
            translate += 1
        if tts_status(state, seg) in (MISSING, STALE):
            dub += 1
    return {"translate": translate, "dub": dub}
