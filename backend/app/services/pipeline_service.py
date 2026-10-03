"""
Full video processing pipeline service.

Runs separation → ASR → translation → voice cloning → TTS → export entirely on
the server side, reporting progress via a callback function, and supports
resuming from the last completed phase on retry.

Designed for a small host (2 GB / single core / no GPU):

* Vocal separation is OFF by default and the torch-based module is never
  imported unless it is explicitly enabled *and* installed.
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
    enable_bgm_separation: Optional[bool] = None,
    enable_voice_clone: bool = False,
    export_video: bool = True,
):
    """
    Execute the full processing pipeline for a video.
    Automatically resumes from the last completed phase.

    Args:
        video_id: MD5 hash of the uploaded video.
        target_language: Target language for translation (e.g. "English").
        server_url_base: Base URL for serving audio files to ASR.
        emit: async callable(event_dict) to push SSE events to the client.
        enable_bgm_separation: Run vocal separation. Defaults to
            config.ENABLE_BGM_SEPARATION_DEFAULT.
        enable_voice_clone: Clone each speaker's voice before synthesis.
        export_video: Mux the dubbed audio back into a downloadable MP4.
    """
    state = get_state(video_id)
    if not state:
        await emit({"error": "Video not found"})
        return

    if enable_bgm_separation is None:
        enable_bgm_separation = config.ENABLE_BGM_SEPARATION_DEFAULT

    # Separation needs torch; if the deps are missing, degrade instead of crashing.
    if enable_bgm_separation:
        from app.services import separation_service

        if not separation_service.is_available():
            logger.warning(
                "[%s] BGM separation requested but audio-separator is not "
                "installed (it requires PyTorch). Continuing without separation.",
                video_id,
            )
            await emit(
                {
                    "phase": "separation",
                    "status": "unavailable",
                    "message": (
                        "Vocal separation is not installed on this server "
                        "(requires PyTorch). Continuing without it."
                    ),
                }
            )
            enable_bgm_separation = False

    # Persist switch settings into state
    state.enable_bgm_separation = enable_bgm_separation
    state.enable_voice_clone = enable_voice_clone
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
        # Phase 0: Vocal Separation (optional, disabled by default)
        # =====================================================
        audio_path = os.path.join(video_dir, "extracted_audio.wav")
        vocals_path = os.path.join(video_dir, "vocals.wav")
        background_path = os.path.join(video_dir, "background.wav")

        if not enable_bgm_separation:
            # Separation was either disabled or already performed in the
            # browser (see POST /api/videos/{id}/stems), which drops
            # vocals.wav + background.wav into the video directory.
            if not os.path.exists(audio_path):
                await asyncio.get_event_loop().run_in_executor(
                    None, asr_service.extract_audio, state.file_path, audio_path
                )
            state.audio_path = audio_path
            save_state(state)

            if os.path.exists(vocals_path) and os.path.exists(background_path):
                await emit({
                    "phase": "separation",
                    "status": "done",
                    "source": "client",
                    "background_url": f"/api/videos/{video_id}/audio/background",
                })
            else:
                await emit({"phase": "separation", "status": "skipped"})
        elif resume_phase == "separation":
            await emit({"phase": "separation", "status": "started"})

            if not os.path.exists(audio_path):
                await asyncio.get_event_loop().run_in_executor(
                    None, asr_service.extract_audio, state.file_path, audio_path
                )
            state.audio_path = audio_path
            save_state(state)

            if os.path.exists(vocals_path) and os.path.exists(background_path):
                await emit({"phase": "separation", "progress": 100})
            else:
                loop = asyncio.get_event_loop()
                progress_queue: asyncio.Queue = asyncio.Queue()

                def on_sep_progress(current: int, total: int):
                    pct = int(current / total * 100) if total > 0 else 0
                    loop.call_soon_threadsafe(progress_queue.put_nowait, pct)

                # Run separation in thread pool
                from app.services import separation_service

                sep_task = asyncio.ensure_future(
                    loop.run_in_executor(
                        None,
                        lambda: separation_service.separate_audio(
                            audio_path, video_dir, progress_callback=on_sep_progress
                        ),
                    )
                )

                while not sep_task.done():
                    try:
                        pct = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                        await emit({"phase": "separation", "progress": pct})
                    except asyncio.TimeoutError:
                        continue

                # Drain queue
                while not progress_queue.empty():
                    pct = progress_queue.get_nowait()
                    await emit({"phase": "separation", "progress": pct})

                # Raise if separation failed
                sep_task.result()

            await emit({
                "phase": "separation",
                "status": "done",
                "background_url": f"/api/videos/{video_id}/audio/background",
            })
        else:
            await emit({
                "phase": "separation",
                "status": "skipped",
                "background_url": f"/api/videos/{video_id}/audio/background",
            })

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

            results = await llm_service.translate_script(video_id, context, target_language)

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
                    voice_id = await voice_clone_service.clone_voice_for_speaker(
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
        for seg in state.segments:
            if enable_voice_clone and voice_clone_service.get_cloned_voice(video_id, seg.speaker_id):
                continue
            tts_service.assign_voice_for_speaker(video_id, seg.speaker_id)

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
                    voice = tts_service.assign_voice_for_speaker(video_id, seg.speaker_id)

                try:
                    audio_file_path = await tts_service.synthesize_speech(
                        video_id,
                        seg.id,
                        seg.translated_text,
                        voice=voice,
                        write_registry=False,
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
