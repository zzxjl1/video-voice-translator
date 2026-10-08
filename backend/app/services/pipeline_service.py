"""
Full video processing pipeline service.

Runs separation → ASR → translation → voice cloning → TTS → export, reporting
progress via a callback function, and supports resuming from the last completed
phase on retry.

Designed for a small host (2 GB / single core / no GPU):

* Vocal separation never runs here. It is either done in the browser (MDX-Net
  over WebGPU/WASM, with the stems uploaded to us).
  Do not reintroduce a local (torch) separator.
* ASR / translation / TTS / cloning are all remote calls, so the local CPU
  only orchestrates.
* TTS runs several segments concurrently — it is network-bound, not CPU-bound.
* Export copies the video stream instead of re-encoding it.
"""
import asyncio
import hashlib
import json
import logging
import os
from typing import Callable, Optional

from app import config
from app.models import (
    Segment,
    Speaker,
    VideoState,
    VideoStatus,
    get_state,
    get_video_dir,
    save_state,
)
from app.services import (
    asr_service,
    speech_timing,
    llm_service,
    tts_service,
    voice_clone_service,
)

logger = logging.getLogger(__name__)


def _fingerprint(*parts: str) -> str:
    """设置指纹：只用于"这批产物还是用当前设置做的吗"这一件事。"""
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _remove_artifact(video_dir: str, name: str) -> None:
    """删掉一个阶段产物（不存在就忽略）。"""
    try:
        os.remove(os.path.join(video_dir, name))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"[pipeline] could not remove {name}: {e}")


def _detect_resume_phase(video_dir: str, state: VideoState) -> str:
    """
    Detect which phase to resume from based on existing files.
    Returns: 'separation', 'asr', 'translation', 'tts', or 'done'.
    """
    has_vocals = os.path.exists(os.path.join(video_dir, "vocals.wav"))
    has_background = os.path.exists(os.path.join(video_dir, "background.wav"))
    has_asr = os.path.exists(os.path.join(video_dir, "asr_result.json"))
    has_translation = os.path.exists(os.path.join(video_dir, "translation_result.json"))
    has_tts = os.path.exists(os.path.join(video_dir, "tts_results.json"))

    # 有行还没译文（上一次翻译部分失败）→ 必须回到翻译阶段。
    # 否则"部分失败"会永久固化：文件在 → 判为已翻译 → 那些行再也没人补。
    if has_asr and state.segments and any(
        (seg.text or "").strip() and not seg.muted and not seg.translated_text
        for seg in state.segments
    ):
        return "translation"

    # Check from the end backwards
    if has_tts and state.segments and all(
        seg.audio_path for seg in state.segments if seg.translated_text and not seg.muted
    ):
        return "done"
    if has_translation and state.segments and any(seg.translated_text for seg in state.segments):
        return "tts"
    if has_asr and state.segments:
        return "translation"
    if has_vocals and has_background:
        return "asr"
    return "separation"


def _invalidate_from_translation(video_dir: str, state: VideoState) -> None:
    """
    "从翻译重做"之前，把要重做的产物作废掉。

    两件事缺一不可：
      1. 清掉内存里的译文与配音路径（`save_state` 不存 segments，所以磁盘上的依据
         是下面两个文件）；
      2. 删掉 translation_result.json / tts_results.json 与该批 mp3 —— 它们是
         `_detect_resume_phase` 与 `load_state` 判断"做过了"的唯一依据。留着 mp3
         更糟：`load_state` 在 tts_results.json 缺失时会**扫描 tts/ 目录**把
         audio_path 找回来，TTS 阶段于是整批跳过。
    """
    for seg in state.segments:
        seg.translated_text = ""
        seg.audio_path = None
        mp3 = os.path.join(video_dir, "tts", f"{seg.id}.mp3")
        if os.path.exists(mp3):
            try:
                os.remove(mp3)
            except OSError as e:
                logger.warning(f"could not remove {mp3}: {e}")

    for name in ("translation_result.json", "tts_results.json"):
        path = os.path.join(video_dir, name)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                logger.warning(f"could not remove {path}: {e}")

    # 上一轮留下的缺口清单对新的一轮没有意义。
    state.failed_translation_ids = []
    state.failed_audio_ids = []


async def run_pipeline(
    video_id: str,
    target_language: str,
    server_url_base: str,
    emit: Callable,
    separation_mode: Optional[str] = None,
    enable_voice_clone: bool = False,
    accent: Optional[str] = None,
    custom_prompt: str = "",
    mm_enhance: bool = False,
    clone_smart_pick: bool = False,
    should_cancel: Optional[Callable[[], bool]] = None,

    # "从哪一步开始重做"，见 schemas.ProcessRequest.start_from。None = 按磁盘自动判定。
    start_phase: str | None = None,
):
    """
    Execute the full processing pipeline for a video.
    Automatically resumes from the last completed phase.

    Args:
        video_id: MD5 hash of the uploaded video.
        target_language: Target language for translation (e.g. "English").
        server_url_base: Base URL for serving audio files to ASR.
        emit: async callable(event_dict) to push SSE events to the client.
        separation_mode: "client" (browser) or "off". Defaults
            to config.SEPARATION_MODE. Validated against the backends that are
            actually usable; an unusable choice degrades to "off" and reports
            the reason.
        should_cancel: optional predicate, polled at every step boundary.
            Cancellation is COOPERATIVE and checked between requests, never
            inside one: a running ASR/TTS/omni call is allowed to finish (the
            provider already billed it, and killing it mid-flight can leave
            half-written files). What this buys is that the pipeline never
            STARTS another step after the user says stop — the granularity is
            "one provider request", which is the smallest unit that can be
            stopped safely anyway. On cancellation the project is left in
            CANCELLED with everything completed so far on disk, so a later run
            resumes from the last finished phase.
        enable_voice_clone: Clone each speaker's voice before synthesis.
        accent: Chinese dialect for the dubbed audio (e.g. "广东话"). None or ""
            means Mandarin, which needs no instruction at all.

    The pipeline ENDS at synthesis. Muxing the dub back into a video is a
    separate, user-triggered step (POST /videos/{id}/export): it used to run
    automatically as a final phase here, which meant a re-process could not
    finish without also producing — and, in the UI, pushing — a file the user
    had not asked for yet. Export is a decision, not a stage.
    """
    state = get_state(video_id)
    if not state:
        await emit({"error": "Video not found"})
        return

    def _cancelled() -> bool:
        return bool(should_cancel is not None and should_cancel())

    async def _stop_if_cancelled(step: str) -> bool:
        """
        步进检查点：用户要求停，就停在这一步之前。

        取消前把状态写成 CANCELLED 并落盘 —— 各阶段产物（asr_result /
        translation_result / tts_results …）本来就是逐阶段写的，所以"取消"天然
        可续：下一次运行按磁盘内容算出 resume_phase，从这里接着走。
        """
        if not _cancelled():
            return False
        logger.info(f"[{video_id}] Cancelled by request, stopping before: {step}")
        state.status = VideoStatus.CANCELLED
        state.cancelled_step = step
        save_state(state)
        await emit({"phase": step, "status": "cancelled"})
        await emit({"cancelled": True, "step": step, "done": True})
        return True

    # Resolved once, here. `config.tts_instruction` both checks the accent
    # against the documented dialect list and drops it for a non-Chinese
    # target, so the synthesis path below does not have to know either rule.
    instruction = config.tts_instruction(target_language, accent)

    # ------------------------------------------------------------------
    # Resolve the separation backend for this job.
    #
    # Separation never runs locally: this host has no GPU and the torch-based
    # backend was removed. It happens in the browser, which POSTs the stems to
    # /api/videos/{id}/stems (dropping vocals.wav + background.wav into the
    # video directory).
    #
    # The client's choice is a *request*: it is validated against
    # config.separation_capabilities() so a stale or hand-crafted value can
    # never send us down a path that cannot work.
    # ------------------------------------------------------------------
    separation_mode = (separation_mode or config.SEPARATION_MODE or "off").lower()
    if separation_mode == "api":
        # 旧工程里存着 "api"（302.AI，已移除）。直接当未知会掉到 "off"，那是
        # "这次不分离"，而用户当初选的是"要分离" —— 映射到浏览器后端更贴近
        # 原意，并会随下面的"Persist settings"落盘纠正。
        logger.warning(
            "[%s] separation_mode 'api' (302.AI) is gone; using 'client' "
            "(in-browser separation) instead.",
            video_id,
        )
        separation_mode = "client"
    if separation_mode not in config.VALID_SEPARATION_MODES:
        logger.warning(
            "[%s] Unknown separation_mode %r, falling back to 'off'",
            video_id,
            separation_mode,
        )
        separation_mode = "off"

    if separation_mode != "off":
        capability = config.separation_capabilities().get(
            separation_mode, {"available": False, "reason": "unknown mode"}
        )
        if not capability["available"]:
            # Degrade instead of failing: say which backend was requested and
            # why it cannot run, then carry on with the original mixed audio.
            logger.warning(
                "[%s] Separation backend %r unavailable (%s); continuing without it.",
                video_id,
                separation_mode,
                capability["reason"],
            )
            await emit(
                {
                    "phase": "separation",
                    "status": "unavailable",
                    "mode": separation_mode,
                    "message": (
                        f"Vocal separation backend '{separation_mode}' is "
                        f"unavailable: {capability['reason']}"
                    ),
                }
            )
            separation_mode = "off"

    # Persist settings into state
    state.separation_mode = separation_mode
    state.enable_voice_clone = enable_voice_clone
    # A pinned voice belongs to a LANGUAGE. Voice ids are language-specific, so
    # a pin from a previous Chinese run is not just a poor fit for an English
    # dub — the service rejects the pair, and it does so part-way through
    # synthesizing rather than at the start. Dropping the pins on a change keeps
    # the failure from ever being reachable.
    if state.target_language and state.target_language != target_language:
        tts_service.clear_speaker_voices(video_id)

    # Kept for the subtitle tracks: they need an ISO language tag so a player
    # can auto-select a track, and the tag is not recoverable from the segments.
    state.target_language = target_language
    # Same reasoning, one field over: the client sends the accent per request,
    # so without storing it a recovered session could not reproduce the dialect
    # this dub was requested in — a refit would fall back to its own default.
    state.accent = accent
    # 同理：这两个开关决定 TTS 前处理与克隆选材的行为，恢复会话后重配音的
    # 行必须与当初一致。
    state.mm_enhance = mm_enhance
    state.clone_smart_pick = clone_smart_pick
    # 自定义翻译要求同理持久化：reprocess 从头重翻，恢复会话也一样，都必须
    # 重放当初注入的要求。空串表示未注入（覆盖旧值，允许"清空后重跑"）。
    state.custom_prompt = custom_prompt or ""
    save_state(state)

    video_dir = get_video_dir(video_id)

    # Clear error state on retry
    if state.status == VideoStatus.ERROR:
        state.error_message = None
        save_state(state)

    # 新的一次运行开始了：上一次的"取消在哪一步"到此为止。
    state.cancelled_step = None

    # ---- 设置指纹：换了设置就作废受影响的产物 ----
    #
    # `_detect_resume_phase` 只看"文件在不在"，所以改了目标语言/口音/增强开关后
    # 再跑，它仍然认为"译文已做完、音频已合成" —— 于是产出还是旧语言，且报成功。
    # 前端用 resetVideo 兜住了自己那条路，但任何别的调用方（脚本、重试逻辑、以后
    # 的 API）都会踩。指纹比对把这件事交给服务端自己判断。
    new_translation_fp = _fingerprint(target_language, str(bool(mm_enhance)), custom_prompt)
    new_tts_fp = _fingerprint(target_language, accent or "", str(bool(enable_voice_clone)))
    if state.translation_fingerprint and state.translation_fingerprint != new_translation_fp:
        logger.warning(
            "[%s] 设置变了（语言/增强/自定义要求）：旧译文与旧配音作废，回到翻译阶段",
            video_id,
        )
        for seg in state.segments:
            seg.translated_text = ""
            seg.audio_path = None
        _remove_artifact(video_dir, "translation_result.json")
        _remove_artifact(video_dir, "tts_results.json")
        state.failed_translation_ids = []
        await emit({"phase": "translation", "status": "invalidated",
                    "reason": "settings changed"})
    elif state.tts_fingerprint and state.tts_fingerprint != new_tts_fp:
        logger.warning(
            "[%s] 配音设置变了（口音/克隆）：译文保留，旧配音作废", video_id
        )
        for seg in state.segments:
            seg.audio_path = None
        _remove_artifact(video_dir, "tts_results.json")
        await emit({"phase": "tts", "status": "invalidated",
                    "reason": "dubbing settings changed"})
    state.translation_fingerprint = new_translation_fp
    state.tts_fingerprint = new_tts_fp
    save_state(state)

    resume_phase = _detect_resume_phase(video_dir, state)

    if start_phase == "translation":
        # 调用方要求"从翻译开始重做"（Reprocess）。前面几步用磁盘上的现成产物，
        # 翻译与配音重做 —— 关键是**真的重做**：
        #   · 只改 resume_phase 不够：翻译阶段"作废旧配音"只在译文真的变了时触发，
        #     同样的设置重跑往往一字不差 → 旧音频还在 audio_path 上 → TTS 阶段因为
        #     "已有音频"而整批跳过，用户点了 Reprocess 却什么都没发生；
        #   · tts_results.json 也必须删：它和 tts/*.mp3 是 _detect_resume_phase 与
        #     load_state 判断"配音做过了"的依据，留着会让中途崩掉的这次运行在下次
        #     被当成"已完成"。
        resume_phase = "translation"
        _invalidate_from_translation(video_dir, state)
        logger.info(
            f"[{video_id}] start_from=translation: skipping separation/ASR/enhance, "
            f"re-doing translation + synthesis for {len(state.segments)} segment(s)"
        )
        await emit({"start_from": "translation"})

    logger.info(f"[{video_id}] Pipeline starting. Resume phase: {resume_phase}")
    await emit({"resume_phase": resume_phase})

    try:
        if await _stop_if_cancelled("separation"):
            return

        # =====================================================
        # Phase 0: Vocal Separation
        #
        # Nothing runs locally. "client" means the browser already produced the
        # stems and uploaded them; "off" does nothing. There is no torch path any
        # more — this host has no GPU.
        # =====================================================
        audio_path = os.path.join(video_dir, "extracted_audio.wav")
        vocals_path = os.path.join(video_dir, "vocals.wav")
        background_path = os.path.join(video_dir, "background.wav")
        background_url = f"/api/videos/{video_id}/audio/background"

        # Extraction is needed by ASR, separation and export alike, so do it once.
        if not os.path.exists(audio_path):
            await asyncio.get_event_loop().run_in_executor(
                None, asr_service.extract_audio, state.file_path, audio_path
            )
        state.audio_path = audio_path
        save_state(state)

        if separation_mode == "client":
            # The browser ran MDX-Net (WebGPU/WASM) and POSTed the stems to
            # /api/videos/{id}/stems, which drops both files into video_dir.
            if os.path.exists(vocals_path) and os.path.exists(background_path):
                await emit({
                    "phase": "separation",
                    "status": "done",
                    "mode": "client",
                    "background_url": background_url,
                })
            else:
                await emit({
                    "phase": "separation",
                    "status": "skipped",
                    "mode": "client",
                    "message": (
                        "the browser did not upload separation stems; "
                        "using the original mixed audio"
                    ),
                })
        else:
            await emit({"phase": "separation", "status": "skipped", "mode": "off"})

        if await _stop_if_cancelled("asr"):
            return

        # =====================================================
        # Phase 1: ASR Transcription
        # =====================================================
        if resume_phase in ("separation", "asr"):
            await emit({"phase": "asr", "status": "started"})

            state.status = VideoStatus.EXTRACTING_AUDIO
            save_state(state)

            # Ensure audio is extracted
            if not os.path.exists(audio_path):
                await asyncio.get_event_loop().run_in_executor(
                    None, asr_service.extract_audio, state.file_path, audio_path
                )

            # Use vocals for ASR if available
            if os.path.exists(vocals_path):
                state.audio_path = vocals_path
            else:
                state.audio_path = audio_path

            state.status = VideoStatus.TRANSCRIBING
            save_state(state)

            file_serve_url = f"{server_url_base}/api/videos/{video_id}/audio"

            segments = await asr_service.transcribe_video(
                video_id, state.file_path, audio_path, file_serve_url
            )

            state.segments = segments
            if not state.speakers:
                unique_labels = sorted(set(seg.speaker_label for seg in segments))
                state.speakers = [Speaker(id=label, name=label) for label in unique_labels]

            state.status = VideoStatus.TRANSCRIBED
            save_state(state)

            # Send segments to frontend
            # muted/hidden 必须一起发：它们是独立文件 segment_flags.json 里的
            # 事实，而前端 mapServerSegment 对缺失字段默认 false —— 只发基础字段
            # 会让"恢复/续跑后界面显示未静音、导出却按静音处理"（P0 #8）。
            segments_data = [
                {
                    "id": seg.id,
                    "speaker_id": seg.speaker_id,
                    "speaker_label": seg.speaker_label,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "text": seg.text,
                    "muted": seg.muted,
                    "hidden": seg.hidden,
                }
                for seg in segments
            ]
            speakers_data = [{"id": s.id, "name": s.name} for s in state.speakers]

            await emit({
                "phase": "asr",
                "status": "done",
                "segments": segments_data,
                "speakers": speakers_data,
            })
        else:
            # Reload segments from state for subsequent phases
            segments_data = [
                {
                    "id": seg.id,
                    "speaker_id": seg.speaker_id,
                    "speaker_label": seg.speaker_label,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "text": seg.text,
                    "translated_text": seg.translated_text,
                    "muted": seg.muted,
                    "hidden": seg.hidden,
                }
                for seg in state.segments
            ]
            speakers_data = [{"id": s.id, "name": s.name} for s in state.speakers]

            await emit({
                "phase": "asr",
                "status": "skipped",
                "segments": segments_data,
                "speakers": speakers_data,
            })

        if await _stop_if_cancelled("mm_enhance"):
            return

        # =====================================================
        # Phase 1.5: Multimodal enhancement (omni reviews the ASR result)
        # =====================================================
        text_overrides: dict[str, str] = {}
        if state.mm_enhance and resume_phase in ("separation", "asr", "translation"):
            await emit({
                "phase": "mm_enhance",
                "status": "started",
                "count": len(state.segments),
            })
            try:
                from app.services import omni_service
                # 落盘缓存：恢复/重试时不再重复调用 omni（听全片不便宜）。
                enh_path = os.path.join(get_video_dir(video_id), "enhanced_transcript.json")
                cached: dict | None = None
                try:
                    with open(enh_path, encoding="utf-8") as f:
                        cached = json.load(f)
                except (OSError, json.JSONDecodeError):
                    cached = None

                if cached and isinstance(cached.get("lines"), dict):
                    text_overrides = {
                        str(k): str(v) for k, v in cached["lines"].items()
                    }
                    logger.info(
                        f"[{video_id}] Multimodal enhancement reused from disk "
                        f"({len(text_overrides)} line(s))"
                    )
                else:
                    await emit({"phase": "mm_enhance", "status": "listening"})
                    audio_url = f"{server_url_base}/api/videos/{video_id}/audio"
                    enhanced = await omni_service.enhance_transcript(
                        video_id=video_id,
                        items=[{"id": seg.id, "text": seg.text} for seg in state.segments],
                        audio_url=audio_url,
                    )
                    text_overrides = {
                        seg.id: enhanced[seg.id]
                        for seg in state.segments
                        if seg.id in enhanced and enhanced[seg.id] != seg.text
                    }
                    try:
                        os.makedirs(os.path.dirname(enh_path), exist_ok=True)
                        with open(enh_path, "w", encoding="utf-8") as f:
                            json.dump({"lines": text_overrides}, f, ensure_ascii=False, indent=1)
                    except OSError as e:
                        logger.warning(f"[{video_id}] Failed to save enhanced transcript: {e}")
                    logger.info(
                        f"[{video_id}] Multimodal enhancement adjusted "
                        f"{len(text_overrides)}/{len(state.segments)} line(s)"
                    )
                await emit({
                    "phase": "mm_enhance",
                    "status": "done",
                    "adjusted": len(text_overrides),
                })
            except Exception as e:
                logger.warning(
                    f"[{video_id}] Multimodal enhancement failed "
                    f"(continuing with the raw ASR text): {e}"
                )
                text_overrides = {}
                await emit({
                    "phase": "mm_enhance",
                    "status": "failed",
                    "error": str(e)[:120],
                })

        if await _stop_if_cancelled("translation"):
            return

        # =====================================================
        # Phase 2: Translation
        # =====================================================
        if resume_phase in ("separation", "asr", "translation"):
            await emit({"phase": "translation", "status": "started", "count": len(state.segments)})

            state.status = VideoStatus.TRANSLATING
            save_state(state)

            # `end_time` is required so the translator can size each line to
            # the time slot it has to fill. When multimodal enhancement produced
            # a corrected+annotated version of a line, THAT text is what gets
            # translated — the raw ASR text stays untouched in state/编辑器.
            context = [
                {
                    "id": seg.id,
                    "text": text_overrides.get(seg.id, seg.text),
                    "speaker_id": seg.speaker_id,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                }
                for seg in state.segments
            ]

            results, failed_ids = await llm_service.translate_script(
                video_id, context, target_language, emit=emit,
                text_overrides=text_overrides or None,
                custom_prompt=custom_prompt,
            )

            # Update state with translations
            result_map = {r["id"]: r["translatedText"] for r in results}
            changed = 0
            for seg in state.segments:
                new_text = result_map.get(seg.id)
                if not new_text or new_text == seg.translated_text:
                    continue
                seg.translated_text = new_text
                # 译文变了 → 这一行的旧配音作废。以前只在 resume_phase=="tts"
                # 时才过滤已有音频，于是"从翻译跑下来"会把每一行重做一遍；
                # 真正危险的是反面：改了语言/文本却留着旧音频（成片还是旧语言）。
                if seg.audio_path:
                    seg.audio_path = None
                    changed += 1
            state.failed_translation_ids = failed_ids
            if changed:
                logger.info(f"[{video_id}] {changed} line(s) changed text — their audio is invalidated")
            if failed_ids:
                logger.warning(
                    f"[{video_id}] {len(failed_ids)} segment(s) have NO translation: "
                    f"{failed_ids[:8]}"
                )
                await emit({
                    "phase": "translation",
                    "status": "incomplete",
                    "failed": failed_ids,
                    "count": len(failed_ids),
                })

            state.status = VideoStatus.TRANSLATED
            save_state(state)
            # 译文自己落盘一份：`_detect_resume_phase` 就是靠这个文件判断"翻译
            # 做完了"，而此前它只由 llm_service.translate_script 顺手写 —— 于是
            # "能不能续跑"取决于另一个模块的副作用。内容一致，重复写是幂等的。
            try:
                with open(
                    os.path.join(video_dir, "translation_result.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(
                        [
                            {"id": seg.id, "translatedText": seg.translated_text}
                            for seg in state.segments
                            if seg.translated_text
                        ],
                        f,
                        ensure_ascii=False,
                        indent=1,
                    )
            except OSError as e:
                logger.warning(f"[{video_id}] Failed to save translation result: {e}")

            translations_data = [
                {"id": r["id"], "translated_text": r["translatedText"]}
                for r in results
            ]
            await emit({
                "phase": "translation",
                "status": "done",
                "translations": translations_data,
            })
        else:
            translations_data = [
                {"id": seg.id, "translated_text": seg.translated_text}
                for seg in state.segments
                if seg.translated_text
            ]
            await emit({
                "phase": "translation",
                "status": "skipped",
                "translations": translations_data,
            })

        if await _stop_if_cancelled("voice_clone"):
            return

        # =====================================================
        # Phase 2.5: Voice Cloning (if enabled)
        # =====================================================
        if enable_voice_clone:
            unique_speakers = sorted(set(seg.speaker_id for seg in state.segments))
            await emit({"phase": "voice_clone", "status": "started", "total": len(unique_speakers)})

            for i, spk_id in enumerate(unique_speakers):
                # 每位说话人的克隆是一次独立的 enrollment 请求：这是能中断的
                # 最小粒度，多说话人时不必等整批跑完。
                if await _stop_if_cancelled("voice_clone"):
                    return
                try:
                    logger.info(f"[{video_id}] Cloning voice for speaker {spk_id}")
                    await emit({
                        "phase": "voice_clone",
                        "progress": i + 1,
                        "total": len(unique_speakers),
                        "speaker_id": spk_id,
                        "status": "cloning",
                    })
                    voice_id, reused = await voice_clone_service.clone_voice_for_speaker(
                        video_id=video_id,
                        speaker_id=spk_id,
                        segments=state.segments,
                        server_url_base=server_url_base,
                        emit=emit,
                    )
                    await emit({
                        "phase": "voice_clone",
                        "progress": i + 1,
                        "total": len(unique_speakers),
                        "speaker_id": spk_id,
                        "voice_id": voice_id,
                        "status": "done",
                        # False only when a NEW enrollment happened; the frontend
                        # words the line accordingly so reuse is visible.
                        "reused": reused,
                    })
                except Exception as e:
                    logger.error(f"[{video_id}] Voice clone failed for {spk_id}: {e}")
                    await emit({
                        "phase": "voice_clone",
                        "progress": i + 1,
                        "total": len(unique_speakers),
                        "speaker_id": spk_id,
                        "status": "failed",
                        "error": str(e),
                    })

            await emit({"phase": "voice_clone", "status": "complete"})

        if await _stop_if_cancelled("tts"):
            return

        # =====================================================
        # Phase 3: TTS Synthesis (concurrent — network bound, not CPU bound)
        # =====================================================
        # 静音行跳过合成：它们本来就不该有配音音频。
        # 恒按"还没有音频"过滤：译文变动的行在上一步已经把 audio_path 清掉了，
        # 所以"有音频"就等于"这一行的当前译文已经配过音"。以前这段过滤只在
        # resume_phase=="tts" 时生效，而 "done"（已完成的工程被再处理一次）会
        # 落到"全部重做"—— 覆盖已有 mp3、重复计费。
        to_synthesize = [
            seg
            for seg in state.segments
            if seg.translated_text and not seg.muted and not seg.audio_path
        ]

        total_tts = len(to_synthesize)
        already_done = len([seg for seg in state.segments if seg.translated_text and seg.audio_path])

        await emit({
            "phase": "tts",
            "status": "started",
            "total": total_tts + already_done,
            "already_done": already_done,
            "concurrency": config.TTS_CONCURRENCY,
        })

        state.status = VideoStatus.SYNTHESIZING
        save_state(state)

        # Pre-assign voices to all speakers so each speaker gets a unique voice.
        # If voice cloning is enabled, cloned voices take precedence.
        # The target language matters: system voice IDs are language-specific,
        # so the pool must match the language being synthesized.
        for seg in state.segments:
            if enable_voice_clone and voice_clone_service.get_cloned_voice(video_id, seg.speaker_id):
                continue
            tts_service.assign_voice_for_speaker(video_id, seg.speaker_id, target_language)

        voice_map = tts_service.get_speaker_voice_map(video_id)
        logger.info(f"[{video_id}] Speaker-voice mapping: {voice_map}")

        semaphore = asyncio.Semaphore(max(1, config.TTS_CONCURRENCY))
        counter_lock = asyncio.Lock()
        counters = {"done": already_done}
        succeeded_segments: list[str] = []
        last_tts_error: list[str] = []   # 收尾时用它说明"为什么整批都失败"

        async def synthesize_segment(seg: Segment) -> None:
            async with semaphore:
                # 每次 TTS 请求之前的检查点。已经在跑的请求会跑完（provider 已
                # 经计费，中断还可能留下半个文件），但停下之后不再发新的 ——
                # "每次请求"就是最细的安全粒度。
                if _cancelled():
                    return
                voice = None
                if enable_voice_clone:
                    voice = voice_clone_service.get_cloned_voice(video_id, seg.speaker_id)
                if not voice:
                    voice = tts_service.assign_voice_for_speaker(
                        video_id, seg.speaker_id, target_language
                    )

                try:
                    # Aim this line at its own time slot. `plan_speech_rate` is a
                    # prediction (measured ~11% average error) but it is free —
                    # no extra TTS call — and it stops the residual from
                    # depending on how badly the translation overshot.
                    # `synthesize_speech` then measures the result and corrects
                    # it once if it is off by more than the tolerance.
                    slot = max(
                        0.5, (seg.end_time or 0.0) - (seg.start_time or 0.0)
                    )
                    planned_rate = speech_timing.plan_speech_rate(
                        seg.translated_text, target_language, slot
                    )
                    audio_file_path = await tts_service.synthesize_speech(
                        video_id,
                        seg.id,
                        seg.translated_text,
                        voice=voice,
                        write_registry=False,
                        speech_rate=planned_rate,
                        target_duration=slot,
                        instruction=instruction,
                    )
                    seg.audio_path = audio_file_path
                    succeeded_segments.append(seg.id)
                    # Flush periodically so an interrupted run can still resume
                    # from tts_results.json without redoing everything.
                    if len(succeeded_segments) % 10 == 0:
                        tts_service.sync_registry(video_id, succeeded_segments)
                    event = {
                        "phase": "tts",
                        "segment_id": seg.id,
                        "audio_url": f"/api/videos/{video_id}/tts/{seg.id}",
                    }
                except Exception as e:
                    logger.error(f"[{video_id}] TTS failed for segment {seg.id}: {e}")
                    last_tts_error.append(str(e))
                    event = {
                        "phase": "tts",
                        "segment_id": seg.id,
                        "tts_error": str(e),
                    }

                async with counter_lock:
                    counters["done"] += 1
                    event["progress"] = counters["done"]
                    event["total"] = total_tts + already_done

                await emit(event)

        if to_synthesize:
            await asyncio.gather(*(synthesize_segment(seg) for seg in to_synthesize))

        # Persist the audio registry once, after the concurrent fan-out.
        if succeeded_segments:
            tts_service.sync_registry(video_id, succeeded_segments)

        # 先判取消，再判缺口：用户主动叫停时，状态该是"已取消"而不是"有缺口"——
        # 被中断的运行本来就会缺东西，那不是"产物有问题"。
        if await _stop_if_cancelled("tts"):
            return

        # ---- 缺口检查：任何"该有而没有"的东西都不允许以"完成"收尾 ----
        #
        # 两类缺口都要拦：没有译文（翻译部分失败）与没有配音（合成失败）。
        # 旧行为两者都只写一行 warning 然后照旧 COMPLETED，用户是在成片里才发现
        # 某几句没声 —— 而且没有任何地方告诉他缺的是哪几句。
        no_translation = [
            seg.id
            for seg in state.segments
            if (seg.text or "").strip() and not seg.muted and not seg.translated_text
        ]
        no_audio = [seg.id for seg in (to_synthesize or []) if seg.id not in succeeded_segments]
        if no_translation or no_audio:
            parts: list[str] = []
            if no_translation:
                parts.append(f"{len(no_translation)} 段没有译文")
            if no_audio:
                parts.append(f"{len(no_audio)} 段没有配音")
            detail = last_tts_error[-1] if last_tts_error else ""
            gap = "，".join((no_translation + no_audio)[:6])
            state.status = VideoStatus.ERROR
            state.failed_audio_ids = no_audio
            state.error_message = (
                "；".join(parts)
                + f"：{gap}"
                + (f"（最后错误：{detail[:200]}）" if detail else "")
            )
            save_state(state)
            logger.error(f"[{video_id}] {state.error_message}")
            # 不带 phase 的 error 事件＝管线级失败（前端据此打 ERROR 并保留日志窗）
            await emit({"error": state.error_message})
            return

        # 缺口补齐了：清掉上一次运行留下的清单，免得界面上一直挂着旧缺口。
        state.failed_translation_ids = []
        state.failed_audio_ids = []

        # 先判取消，再宣布完成（P0 #4）。原顺序是"写 COMPLETED → 广播 tts done
        # → 再翻回 CANCELLED"：磁盘上会短暂出现"已完成"，前端已经打过
        # "Audio Synthesis Complete"，任何此刻轮询 /status 的客户端都会读到它。
        state.status = VideoStatus.COMPLETED
        save_state(state)

        await emit({"phase": "tts", "status": "done"})

        if _cancelled():
            # 批次里剩下的请求已经跳过了；把状态收成 CANCELLED 再结束。
            state.status = VideoStatus.CANCELLED
            state.cancelled_step = "tts"
            save_state(state)
            await emit({"phase": "tts", "status": "cancelled"})
            await emit({"cancelled": True, "step": "tts", "done": True})
            return

        # No export phase: the pipeline stops at synthesis. Muxing happens only
        # when the user asks for it (POST /videos/{id}/export), so nothing here
        # produces a video file behind their back — and a re-process is free to
        # end without touching any existing export.
        await emit({"done": True})

    except Exception as e:
        logger.error(f"[{video_id}] Pipeline error: {str(e)}", exc_info=True)
        state.status = VideoStatus.ERROR
        state.error_message = str(e)
        save_state(state)
        await emit({"error": str(e)})
