"""
Full video processing pipeline service.
Runs separation → ASR → translation → TTS entirely on the server side,
reporting progress via a callback function.
Supports resuming from the last completed phase on retry.
"""
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
from app.services import asr_service, llm_service, separation_service, tts_service

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
):
    """
    Execute the full processing pipeline for a video.
    Automatically resumes from the last completed phase.

    Args:
        video_id: MD5 hash of the uploaded video.
        target_language: Target language for translation (e.g. "English").
        server_url_base: Base URL for serving audio files to ASR.
        emit: async callable(event_dict) to push SSE events to the client.
    """
    state = get_state(video_id)
    if not state:
        await emit({"error": "Video not found"})
        return

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
        # =====================================================
        audio_path = os.path.join(video_dir, "extracted_audio.wav")
        vocals_path = os.path.join(video_dir, "vocals.wav")
        background_path = os.path.join(video_dir, "background.wav")

        if resume_phase == "separation":
            await emit({"phase": "separation", "status": "started"})

            if not os.path.exists(audio_path):
                asr_service.extract_audio(state.file_path, audio_path)
            state.audio_path = audio_path
            save_state(state)

            if os.path.exists(vocals_path) and os.path.exists(background_path):
                await emit({"phase": "separation", "progress": 100})
            else:
                import asyncio
                loop = asyncio.get_event_loop()
                progress_queue = asyncio.Queue()

                def on_sep_progress(current: int, total: int):
                    pct = int(current / total * 100) if total > 0 else 0
                    loop.call_soon_threadsafe(progress_queue.put_nowait, pct)

                # Run separation in thread pool
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
                asr_service.extract_audio(state.file_path, audio_path)

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

            context = [
                {
                    "id": seg.id,
                    "text": seg.text,
                    "speaker_id": seg.speaker_id,
                    "start_time": seg.start_time,
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
        # Phase 3: TTS Synthesis
        # =====================================================
        to_synthesize = [seg for seg in state.segments if seg.translated_text]

        if resume_phase == "tts":
            # Only synthesize segments that don't have audio yet
            to_synthesize = [seg for seg in to_synthesize if not seg.audio_path]

        total_tts = len(to_synthesize)
        already_done = len([seg for seg in state.segments if seg.translated_text and seg.audio_path])

        await emit({"phase": "tts", "status": "started", "total": total_tts + already_done, "already_done": already_done})

        state.status = VideoStatus.SYNTHESIZING
        save_state(state)

        for i, seg in enumerate(to_synthesize):
            try:
                audio_file_path = await tts_service.synthesize_speech(
                    video_id, seg.id, seg.translated_text
                )
                seg.audio_path = audio_file_path
                audio_url = f"/api/videos/{video_id}/tts/{seg.id}"
                await emit({
                    "phase": "tts",
                    "progress": already_done + i + 1,
                    "total": total_tts + already_done,
                    "segment_id": seg.id,
                    "audio_url": audio_url,
                })
            except Exception as e:
                logger.error(f"[{video_id}] TTS failed for segment {seg.id}: {e}")
                await emit({
                    "phase": "tts",
                    "progress": already_done + i + 1,
                    "total": total_tts + already_done,
                    "segment_id": seg.id,
                    "tts_error": str(e),
                })

        state.status = VideoStatus.COMPLETED
        save_state(state)

        await emit({"phase": "tts", "status": "done"})
        await emit({"done": True})

    except Exception as e:
        logger.error(f"[{video_id}] Pipeline error: {str(e)}", exc_info=True)
        state.status = VideoStatus.ERROR
        state.error_message = str(e)
        save_state(state)
        await emit({"error": str(e)})
