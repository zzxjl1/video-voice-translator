"""
Full video processing pipeline service.

Runs separation → ASR → translation → voice cloning → TTS → export, reporting
progress via a callback function, and supports resuming from the last completed
phase on retry.

Designed for a small host (2 GB / single core / no GPU):

* Vocal separation never runs here. It is either done in the browser (MDX-Net
  over WebGPU/WASM, with the stems uploaded to us) or by 302.AI over HTTPS.
  Do not reintroduce a local (torch) separator.
* ASR / translation / TTS / cloning are all remote calls, so the local CPU
  only orchestrates.
* TTS runs several segments concurrently — it is network-bound, not CPU-bound.
* Export copies the video stream instead of re-encoding it.
"""
import asyncio
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
    export_service,
    llm_service,
    tts_service,
    voice_clone_service,
)

logger = logging.getLogger(__name__)


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

    # Check from the end backwards
    if has_tts and state.segments and all(seg.audio_path for seg in state.segments if seg.translated_text):
        return "done"
    if has_translation and state.segments and any(seg.translated_text for seg in state.segments):
        return "tts"
    if has_asr and state.segments:
        return "translation"
    if has_vocals and has_background:
        return "asr"
    return "separation"


async def run_pipeline(
    video_id: str,
    target_language: str,
    server_url_base: str,
    emit: Callable,
    separation_mode: Optional[str] = None,
    enable_voice_clone: bool = False,
    export_video: bool = True,
    accent: Optional[str] = None,
):
    """
    Execute the full processing pipeline for a video.
    Automatically resumes from the last completed phase.

    Args:
        video_id: MD5 hash of the uploaded video.
        target_language: Target language for translation (e.g. "English").
        server_url_base: Base URL for serving audio files to ASR.
        emit: async callable(event_dict) to push SSE events to the client.
        separation_mode: "client" (browser), "api" (302.AI) or "off". Defaults
            to config.SEPARATION_MODE. Validated against the backends that are
            actually usable; an unusable choice degrades to "off" and reports
            the reason.
        enable_voice_clone: Clone each speaker's voice before synthesis.
        export_video: Mux the dubbed audio back into a downloadable MP4.
        accent: Chinese dialect for the dubbed audio (e.g. "广东话"). None or ""
            means Mandarin, which needs no instruction at all.
    """
    state = get_state(video_id)
    if not state:
        await emit({"error": "Video not found"})
        return

    # Resolved once, here. `config.tts_instruction` both checks the accent
    # against the documented dialect list and drops it for a non-Chinese
    # target, so the synthesis path below does not have to know either rule.
    instruction = config.tts_instruction(target_language, accent)

    # ------------------------------------------------------------------
    # Resolve the separation backend for this job.
    #
    # Separation never runs locally: this host has no GPU and the torch-based
    # backend was removed. Either the browser already produced the stems
    # (POST /api/videos/{id}/stems drops vocals.wav + background.wav into the
    # video directory) or the 302.AI backend does it over HTTPS.
    #
    # The client's choice is a *request*: it is validated against
    # config.separation_capabilities() so a stale or hand-crafted value can
    # never send us down a path that cannot work.
    # ------------------------------------------------------------------
    separation_mode = (separation_mode or config.SEPARATION_MODE or "off").lower()
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

    enable_bgm_separation = separation_mode != "off"

    # Persist settings into state
    state.enable_bgm_separation = enable_bgm_separation
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
    save_state(state)

    video_dir = get_video_dir(video_id)

    # Clear error state on retry
    if state.status == VideoStatus.ERROR:
        state.error_message = None
        save_state(state)

    resume_phase = _detect_resume_phase(video_dir, state)
    logger.info(f"[{video_id}] Pipeline starting. Resume phase: {resume_phase}")
    await emit({"resume_phase": resume_phase})

    try:
        # =====================================================
        # Phase 0: Vocal Separation
        #
        # Nothing runs locally. "client" means the browser already produced the
        # stems and uploaded them; "api" calls 302.AI over HTTPS; "off" does
        # nothing. There is no torch path any more — this host has no GPU.
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
        elif separation_mode == "api":
            from app.services import api_separation_service

            await emit({"phase": "separation", "status": "started", "mode": "api"})
            try:
                await api_separation_service.separate_audio(
                    video_id=video_id,
                    audio_path=audio_path,
                    video_dir=video_dir,
                    server_url_base=server_url_base,
                    emit=emit,
                )
                await emit({
                    "phase": "separation",
                    "status": "done",
                    "mode": "api",
                    "background_url": background_url,
                })
            except Exception as e:  # noqa: BLE001 - degrade, never kill the job
                logger.error(
                    "[%s] 302.AI separation failed: %s", video_id, e, exc_info=True
                )
                await emit({
                    "phase": "separation",
                    "status": "failed",
                    "mode": "api",
                    "error": str(e),
                    "message": "continuing with the original mixed audio",
                })
        else:
            await emit({"phase": "separation", "status": "skipped", "mode": "off"})

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
            segments_data = [
                {
                    "id": seg.id,
                    "speaker_id": seg.speaker_id,
                    "speaker_label": seg.speaker_label,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                    "text": seg.text,
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

        # =====================================================
        # Phase 2: Translation
        # =====================================================
        if resume_phase in ("separation", "asr", "translation"):
            await emit({"phase": "translation", "status": "started", "count": len(state.segments)})

            state.status = VideoStatus.TRANSLATING
            save_state(state)

            # `end_time` is required so the translator can size each line to
            # the time slot it has to fill.
            context = [
                {
                    "id": seg.id,
                    "text": seg.text,
                    "speaker_id": seg.speaker_id,
                    "start_time": seg.start_time,
                    "end_time": seg.end_time,
                }
                for seg in state.segments
            ]

            results = await llm_service.translate_script(
                video_id, context, target_language, emit=emit
            )

            # Update state with translations
            result_map = {r["id"]: r["translatedText"] for r in results}
            for seg in state.segments:
                if seg.id in result_map:
                    seg.translated_text = result_map[seg.id]

            state.status = VideoStatus.TRANSLATED
            save_state(state)

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

        # =====================================================
        # Phase 2.5: Voice Cloning (if enabled)
        # =====================================================
        if enable_voice_clone:
            unique_speakers = sorted(set(seg.speaker_id for seg in state.segments))
            await emit({"phase": "voice_clone", "status": "started", "total": len(unique_speakers)})

            for i, spk_id in enumerate(unique_speakers):
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

        # =====================================================
        # Phase 3: TTS Synthesis (concurrent — network bound, not CPU bound)
        # =====================================================
        to_synthesize = [seg for seg in state.segments if seg.translated_text]

        if resume_phase == "tts":
            # Only synthesize segments that don't have audio yet
            to_synthesize = [seg for seg in to_synthesize if not seg.audio_path]

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

        async def synthesize_segment(seg: Segment) -> None:
            async with semaphore:
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
                    planned_rate = llm_service.plan_speech_rate(
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

        state.status = VideoStatus.COMPLETED
        save_state(state)

        await emit({"phase": "tts", "status": "done"})

        # =====================================================
        # Phase 4: Export (mux dubbed audio back into the video)
        # =====================================================
        export_url = None
        if export_video and config.EXPORT_ENABLED:
            try:
                if not export_service.is_available():
                    await emit({
                        "phase": "export",
                        "status": "failed",
                        "error": "ffmpeg/ffprobe not available on the server",
                    })
                else:
                    await export_service.export_video(video_id, emit=emit)
                    export_url = f"/api/videos/{video_id}/export/download"
            except Exception as e:
                logger.error(f"[{video_id}] Export failed: {e}", exc_info=True)
                await emit({"phase": "export", "status": "failed", "error": str(e)})

        await emit({"done": True, "export_url": export_url})

    except Exception as e:
        logger.error(f"[{video_id}] Pipeline error: {str(e)}", exc_info=True)
        state.status = VideoStatus.ERROR
        state.error_message = str(e)
        save_state(state)
        await emit({"error": str(e)})
