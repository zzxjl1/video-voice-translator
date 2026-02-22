"""
Video processing API routes.
All endpoints are prefixed with /api/videos.
"""
import hashlib
import logging
import os
import shutil
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from app import config
from app.models import (
    Speaker,
    VideoState,
    VideoStatus,
    get_or_create_state,
    get_state,
    get_video_dir,
    save_state,
)
from app.schemas import (
    SegmentOut,
    SpeakerOut,
    TranscribeResponse,
    TranslateRequest,
    TranslateResponse,
    TranslationResultItem,
    TTSRequest,
    TTSResponse,
    UploadResponse,
    VideoStatusResponse,
    ProcessRequest,
)
from app.services import asr_service, llm_service, separation_service, tts_service
from app.services import pipeline_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["videos"])


def _compute_md5(file_path: str) -> str:
    """Compute MD5 hash of a file."""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


@router.post("/upload", response_model=UploadResponse)
async def upload_video(file: UploadFile = File(...)):
    """Upload a video file. Returns video_id (MD5 hash) and whether it already exists."""
    logger.info(f"Received upload request for file: {file.filename}")
    
    # We need to save to a temp location first to compute MD5
    temp_dir = os.path.join(config.DATA_DIR, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}_{file.filename}")
    
    try:
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        video_id = _compute_md5(temp_path)
        logger.info(f"Computed MD5 for {file.filename}: {video_id}")

        # Final destination
        video_dir = get_video_dir(video_id)
        ext = os.path.splitext(file.filename)[1]
        final_video_name = f"original_video{ext}"
        final_path = os.path.join(video_dir, final_video_name)

        already_exists = os.path.exists(final_path)

        if not already_exists:
            os.rename(temp_path, final_path)
            logger.info(f"Saved new video file: {final_path}")
        else:
            os.remove(temp_path)  # Already uploaded before
            logger.info(f"Video already exists, skipping save: {final_path}")

        # Create/retrieve state
        state = get_or_create_state(video_id, file.filename, final_path)
        save_state(state)

        return UploadResponse(
            video_id=video_id,
            filename=file.filename,
            exists=already_exists,
            status=state.status.value,
        )
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/status", response_model=VideoStatusResponse)
async def get_video_status(video_id: str):
    """Get the current processing status of a video with detailed progress info."""
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    video_dir = get_video_dir(video_id)

    return VideoStatusResponse(
        video_id=state.video_id,
        filename=state.filename,
        status=state.status.value,
        error=state.error_message,
        segments=[
            SegmentOut(
                id=seg.id,
                speaker_id=seg.speaker_id,
                speaker_label=seg.speaker_label,
                start_time=seg.start_time,
                end_time=seg.end_time,
                text=seg.text,
                translated_text=seg.translated_text,
                audio_url=f"/api/videos/{video_id}/tts/{seg.id}" if seg.audio_path else None,
            )
            for seg in state.segments
        ],
        speakers=[
            SpeakerOut(id=s.id, name=s.name)
            for s in state.speakers
        ],
        has_vocals=os.path.exists(os.path.join(video_dir, "vocals.wav")),
        has_background=os.path.exists(os.path.join(video_dir, "background.wav")),
        has_asr=os.path.exists(os.path.join(video_dir, "asr_result.json")),
        has_translation=os.path.exists(os.path.join(video_dir, "translation_result.json")),
        has_tts=os.path.exists(os.path.join(video_dir, "tts_results.json")),
    )


@router.post("/{video_id}/reset")
async def reset_video(video_id: str):
    """
    Reset a video's processing data so it can be re-processed from scratch.
    Deletes all intermediate files but keeps the original video.
    """
    video_dir = get_video_dir(video_id)
    if not os.path.exists(video_dir):
        raise HTTPException(status_code=404, detail="Video not found")

    # Find original video file (keep it)
    original_video = None
    for f in os.listdir(video_dir):
        if f.startswith("original_video"):
            original_video = f
            break

    if not original_video:
        raise HTTPException(status_code=404, detail="Original video file not found")

    # Delete everything except original video
    for item in os.listdir(video_dir):
        if item == original_video:
            continue
        item_path = os.path.join(video_dir, item)
        if os.path.isdir(item_path):
            shutil.rmtree(item_path)
        else:
            os.remove(item_path)

    # Re-create clean state
    original_path = os.path.join(video_dir, original_video)
    state = VideoState(
        video_id=video_id,
        filename=original_video,
        file_path=original_path,
    )
    save_state(state)

    logger.info(f"[{video_id}] Reset complete. All intermediate data removed.")
    return {"video_id": video_id, "status": "reset"}


@router.post("/{video_id}/transcribe", response_model=TranscribeResponse)
async def transcribe_video(video_id: str, request: Request):
    """
    Transcribe a video using Ali DashScope ASR.
    Extracts audio, submits to ASR, polls for results.
    """
    logger.info(f"Transcription requested for video_id: {video_id}")
    state = get_state(video_id)
    if not state:
        logger.warning(f"Transcription failed: Video {video_id} not found")
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        state.status = VideoStatus.EXTRACTING_AUDIO
        logger.info(f"[{video_id}] Status: {state.status.value}")
        save_state(state)

        # Extract audio to the video specific directory
        video_dir = get_video_dir(video_id)
        audio_path = os.path.join(video_dir, "extracted_audio.wav")
        if not os.path.exists(audio_path):
            logger.info(f"[{video_id}] Extracting audio...")
            asr_service.extract_audio(state.file_path, audio_path)

        # Use vocals.wav for ASR if available (better accuracy without background noise)
        vocals_path = os.path.join(video_dir, "vocals.wav")
        if os.path.exists(vocals_path):
            logger.info(f"[{video_id}] Using separated vocals for ASR")
            state.audio_path = vocals_path
        else:
            state.audio_path = audio_path

        state.status = VideoStatus.TRANSCRIBING
        logger.info(f"[{video_id}] Status: {state.status.value}")
        save_state(state)

        # Build a URL for the audio file that Ali ASR can reach
        base_url = config.SERVER_URL_BASE
        file_serve_url = f"{base_url}/api/videos/{video_id}/audio"
        logger.info(f"[{video_id}] Serving audio for ASR at: {file_serve_url}")

        # Submit and poll ASR
        segments = await asr_service.transcribe_video(
            video_id, state.file_path, audio_path, file_serve_url
        )

        state.segments = segments
        
        # Initialize speakers if not already present
        if not state.speakers:
            unique_labels = sorted(list(set(seg.speaker_label for seg in segments)))
            state.speakers = [
                Speaker(id=label, name=label)
                for label in unique_labels
            ]
            
        state.status = VideoStatus.TRANSCRIBED
        logger.info(f"[{video_id}] Status: {state.status.value}. Found {len(segments)} segments.")
        save_state(state)

        return TranscribeResponse(
            video_id=video_id,
            status=state.status.value,
            segments=[
                SegmentOut(
                    id=seg.id,
                    speaker_id=seg.speaker_id,
                    speaker_label=seg.speaker_label,
                    start_time=seg.start_time,
                    end_time=seg.end_time,
                    text=seg.text,
                )
                for seg in segments
            ],
            speakers=[
                SpeakerOut(id=s.id, name=s.name)
                for s in state.speakers
            ]
        )
    except Exception as e:
        logger.error(f"[{video_id}] Transcription error: {str(e)}", exc_info=True)
        state.status = VideoStatus.ERROR
        state.error_message = str(e)
        save_state(state)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{video_id}/translate", response_model=TranslateResponse)
async def translate_video(video_id: str, req: TranslateRequest):
    """Translate transcript segments using SiliconFlow LLM."""
    logger.info(f"Translation requested for video_id: {video_id}, Target: {req.target_language}")
    state = get_state(video_id)
    if not state:
        logger.warning(f"Translation failed: Video {video_id} not found")
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        state.status = VideoStatus.TRANSLATING
        logger.info(f"[{video_id}] Status: {state.status.value}")
        save_state(state)

        # Prepare context payload
        context = [
            {
                "id": seg.id,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "start_time": seg.start_time,
            }
            for seg in req.segments
        ]

        results = await llm_service.translate_script(video_id, context, req.target_language)

        # Update state with translations
        translations = []
        result_map = {r["id"]: r["translatedText"] for r in results}
        for seg in state.segments:
            if seg.id in result_map:
                seg.translated_text = result_map[seg.id]
                translations.append(
                    TranslationResultItem(
                        id=seg.id,
                        translated_text=seg.translated_text,
                    )
                )

        state.status = VideoStatus.TRANSLATED
        logger.info(f"[{video_id}] Status: {state.status.value}. Translated {len(translations)} items.")
        save_state(state)

        return TranslateResponse(
            video_id=video_id,
            translations=translations,
        )
    except Exception as e:
        logger.error(f"[{video_id}] Translation error: {str(e)}", exc_info=True)
        state.status = VideoStatus.ERROR
        state.error_message = str(e)
        save_state(state)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{video_id}/tts", response_model=TTSResponse)
async def synthesize_speech(video_id: str, req: TTSRequest):
    """Synthesize speech for a text segment using OpenAI TTS."""
    logger.info(f"TTS requested for video_id: {video_id}")
    state = get_state(video_id)
    if not state:
        logger.warning(f"TTS failed: Video {video_id} not found")
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        await tts_service.synthesize_speech(
            video_id, req.segment_id, req.text, req.voice
        )
        logger.info(f"[{video_id}] TTS synthesis complete (Segment: {req.segment_id})")

        # Update in-memory state to reflect the new audio path
        audio_path = os.path.join(get_video_dir(video_id), "tts", f"{req.segment_id}.mp3")
        for seg in state.segments:
            if seg.id == req.segment_id:
                seg.audio_path = audio_path
                break
        save_state(state)

        audio_url = f"/api/videos/{video_id}/tts/{req.segment_id}"
        return TTSResponse(
            audio_url=audio_url,
            content_type="audio/mp3",
        )
    except Exception as e:
        logger.error(f"[{video_id}] TTS error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/video")
async def serve_video(video_id: str, request: Request):
    """Serve the original video file with Range request support for seeking."""
    from fastapi.responses import Response, StreamingResponse
    import mimetypes

    state = get_state(video_id)
    if not state or not state.file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    if not os.path.exists(state.file_path):
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    file_path = state.file_path
    file_size = os.path.getsize(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "video/mp4"

    range_header = request.headers.get("range")

    if range_header:
        # Parse Range header: "bytes=start-end"
        range_spec = range_header.strip().split("=")[1]
        range_parts = range_spec.split("-")
        start = int(range_parts[0]) if range_parts[0] else 0
        end = int(range_parts[1]) if range_parts[1] else file_size - 1
        end = min(end, file_size - 1)
        content_length = end - start + 1

        def iter_file():
            with open(file_path, "rb") as f:
                f.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk_size = min(8192, remaining)
                    data = f.read(chunk_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        return StreamingResponse(
            iter_file(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(content_length),
            },
        )
    else:
        # No range requested — serve full file
        from fastapi.responses import FileResponse
        return FileResponse(
            file_path,
            media_type=content_type,
            headers={"Accept-Ranges": "bytes"},
        )


@router.get("/{video_id}/audio")
async def serve_audio(video_id: str):
    """
    Serve the extracted audio file.
    Used internally for Ali ASR to access the audio file via URL.
    """
    from fastapi.responses import FileResponse

    logger.info(f"Audio serve requested for video_id: {video_id}")
    state = get_state(video_id)
    if not state or not state.audio_path:
        logger.warning(f"Audio serve failed: No state or audio path for {video_id}")
        raise HTTPException(status_code=404, detail="Audio not found")

    if not os.path.exists(state.audio_path):
        logger.error(f"Audio serve failed: File not found on disk at {state.audio_path}")
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    return FileResponse(state.audio_path, media_type="audio/wav")


@router.post("/{video_id}/separate")
async def separate_vocals(video_id: str):
    """
    Separate audio into vocals and background with SSE progress streaming.
    Returns Server-Sent Events with progress updates, then the final result.
    """
    import asyncio
    import json as _json
    from fastapi.responses import StreamingResponse

    logger.info(f"Vocal separation requested for video_id: {video_id}")
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    async def event_stream():
        loop = asyncio.get_event_loop()
        progress_queue = asyncio.Queue()

        def on_progress(current: int, total: int):
            pct = int(current / total * 100) if total > 0 else 0
            loop.call_soon_threadsafe(progress_queue.put_nowait, {"progress": pct, "current": current, "total": total})

        async def run_separation():
            video_dir = get_video_dir(video_id)
            audio_path = os.path.join(video_dir, "extracted_audio.wav")
            if not os.path.exists(audio_path):
                logger.info(f"[{video_id}] Extracting audio for separation...")
                asr_service.extract_audio(state.file_path, audio_path)
            state.audio_path = audio_path
            save_state(state)

            result = await loop.run_in_executor(
                None,
                lambda: separation_service.separate_audio(audio_path, video_dir, progress_callback=on_progress),
            )
            return result

        sep_task = asyncio.create_task(run_separation())

        # Stream progress events until separation completes
        while not sep_task.done():
            try:
                msg = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                yield f"data: {_json.dumps(msg)}\n\n"
            except asyncio.TimeoutError:
                continue

        # Drain remaining progress messages
        while not progress_queue.empty():
            msg = progress_queue.get_nowait()
            yield f"data: {_json.dumps(msg)}\n\n"

        try:
            result = await sep_task
            final = {
                "done": True,
                "video_id": video_id,
                "vocals": result["vocals"],
                "background": result["background"],
                "background_url": f"/api/videos/{video_id}/audio/background",
            }
            yield f"data: {_json.dumps(final)}\n\n"
        except Exception as e:
            logger.error(f"[{video_id}] Separation error: {str(e)}", exc_info=True)
            yield f"data: {_json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/{video_id}/process")
async def process_video(video_id: str, req: ProcessRequest):
    """
    Run the full processing pipeline (separation → ASR → translation → TTS)
    with SSE progress streaming. Frontend only needs to call this once after upload.
    """
    import asyncio
    import json as _json
    from fastapi.responses import StreamingResponse

    logger.info(f"Full pipeline requested for video_id: {video_id}, lang: {req.target_language}")
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    async def event_stream():
        queue = asyncio.Queue()

        async def emit(event: dict):
            await queue.put(event)

        async def run():
            try:
                await pipeline_service.run_pipeline(
                    video_id=video_id,
                    target_language=req.target_language,
                    server_url_base=config.SERVER_URL_BASE,
                    emit=emit,
                )
            except Exception as e:
                logger.error(f"[{video_id}] Pipeline error: {e}", exc_info=True)
                await queue.put({"error": str(e)})
            finally:
                await queue.put(None)  # sentinel

        task = asyncio.create_task(run())

        while True:
            msg = await queue.get()
            if msg is None:
                break
            yield f"data: {_json.dumps(msg)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{video_id}/audio/background")
async def serve_background_audio(video_id: str):
    """Serve the separated background (instrumental) audio."""
    from fastapi.responses import FileResponse

    video_dir = get_video_dir(video_id)
    bg_path = os.path.join(video_dir, "background.wav")

    if not os.path.exists(bg_path):
        raise HTTPException(status_code=404, detail="Background audio not found")

    return FileResponse(bg_path, media_type="audio/wav")


@router.get("/{video_id}/tts/{segment_id}")
async def serve_segment_audio(video_id: str, segment_id: str):
    """Serve synthesized MP3 for a specific segment."""
    from fastapi.responses import FileResponse
    video_dir = get_video_dir(video_id)
    audio_path = os.path.join(video_dir, "tts", f"{segment_id}.mp3")
    
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Segment audio not found")
        
    return FileResponse(audio_path, media_type="audio/mp3")
