"""
客户端看到的项目视图，以及两份视图之间的增量。

为什么在服务端算好、而不是让前端拿原始文档自己算：行是否"过期"取决于指纹，指纹
取决于音色解析、方言指令、TTS 模型……这套逻辑只该有一份（`app/status.py`）。
客户端收到的是现成的 `trStatus` / `ttsStatus` / `audioUrl`，应用增量即可。

字段用 camelCase（前端 TS 的惯例）；服务端内部仍是 snake_case，翻译只发生在这一层。
"""
import copy
import os
from typing import Optional

from app import config, status
from app.models import Segment, Speaker, VideoState, video_dir_path

# 增量里这些顶层键变了就整块发（体积小）；segments 单独按行比较。
_BLOCK_KEYS = (
    "stage", "initError", "initReport", "settings", "speakers", "subtitle",
    "needs", "hasStems", "filename", "chatConv",
)


def settings_view(state: VideoState) -> dict:
    return {
        "targetLanguage": state.target_language,
        "accent": state.accent,
        "separationMode": state.separation_mode,
        "enableVoiceClone": state.enable_voice_clone,
        "enableDubbing": state.enable_dubbing,
        "mmEnhance": state.mm_enhance,
        "cloneSmartPick": state.clone_smart_pick,
        "customPrompt": state.custom_prompt,
    }


def speaker_view(state: VideoState, speaker: Speaker) -> dict:
    return {
        "id": speaker.id,
        "name": speaker.name,
        "voice": speaker.voice,
        "voiceLabel": config.voice_label(speaker.voice) if speaker.voice else None,
        "cloned": status.clone_voice_id(state, speaker) is not None,
        "lines": sum(1 for s in state.segments if s.speaker_id == speaker.id),
    }


def segment_view(state: VideoState, seg: Segment, index: int) -> dict:
    audio_url: Optional[str] = None
    if seg.audio_path:
        # ?v=<指纹>：同一路径被覆盖重写后，浏览器缓存自然失效。
        audio_url = f"/api/projects/{state.video_id}/tts/{seg.id}?v={seg.tts_fp}"
    return {
        "id": seg.id,
        "line": index + 1,                       # 人和 AI 说的"第 N 句"
        "speakerId": seg.speaker_id,
        "start": seg.start_time,
        "end": seg.end_time,
        "text": seg.text,
        "translation": seg.translated_text,
        "reviewedText": seg.reviewed_text,
        "kind": seg.kind,
        "muted": seg.muted,
        "hidden": seg.hidden,
        "ttsVoice": seg.tts_voice,
        "ttsRate": seg.tts_rate,
        "ttsInstruction": seg.tts_instruction,
        "trStatus": status.tr_status(state, seg),
        "trSuspect": status.untranslated_suspect(state, seg),        # 译文看起来没被翻译（还是原文的文字）
        "ttsStatus": status.tts_status(state, seg),
        "audioUrl": audio_url,
        "audioDuration": seg.actual_duration,
    }


def mix_view(state: VideoState) -> Optional[dict]:
    """混音计划（播放与导出共用的唯一策略产物）；没有任何可混的配音时为 None。"""
    from app.services import mix_service
    try:
        plan = mix_service.build_mix_plan(state)
    except RuntimeError:
        return None
    return {"version": plan["version"], "plan": plan}


def state_view(state: VideoState) -> dict:
    """完整视图。`state` 必须已经 hydrate（见 `snapshot`）。"""
    base = video_dir_path(state.video_id)
    return {
        "id": state.video_id,
        "rev": state.rev,
        "filename": state.filename,
        "stage": state.stage,
        "initError": state.init_error,
        "initReport": state.init_report,
        "settings": settings_view(state),
        "speakers": [speaker_view(state, s) for s in state.speakers],
        "segments": [segment_view(state, s, i) for i, s in enumerate(state.segments)],
        "subtitle": {"style": state.subtitle_style, "export": state.subtitle_export},
        "needs": status.needs(state),
        "hasStems": os.path.exists(os.path.join(base, "background.wav")),
        "chatConv": state.chat_conv,
        "mix": mix_view(state),
    }


def snapshot(state: VideoState) -> dict:
    """对任意（未 hydrate 的）状态算出视图，不动传入的对象。"""
    work = copy.deepcopy(state)
    status.hydrate(work)
    return state_view(work)


def diff(old: dict, new: dict) -> dict:
    """两份视图之间的增量；没有变化返回空 dict。"""
    patch: dict = {}

    old_segs = {s["id"]: s for s in old["segments"]}
    new_segs = {s["id"]: s for s in new["segments"]}
    upsert = [s for s in new["segments"] if old_segs.get(s["id"]) != s]
    removed = [i for i in old_segs if i not in new_segs]
    old_order = [s["id"] for s in old["segments"]]
    new_order = [s["id"] for s in new["segments"]]
    if upsert or removed or old_order != new_order:
        patch["segments"] = {
            "upsert": upsert,
            "remove": removed,
            # 顺序（含"第 N 句"的编号）变了才发整张 id 表。
            "order": new_order if old_order != new_order else None,
        }

    for key in _BLOCK_KEYS:
        if old.get(key) != new.get(key):
            patch[key] = new.get(key)

    old_mix, new_mix = old.get("mix"), new.get("mix")
    if (old_mix or {}).get("version") != (new_mix or {}).get("version"):
        patch["mix"] = new_mix
    return patch
