"""
Video processing API routes.
All endpoints are prefixed with /api/videos.
"""
import hashlib
import logging
import os
import shutil
import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile

from app import config
from app.deps import enforce_separator_token
from app.services import token_service
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
    ExportResponse,
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
from app.services import asr_service, export_service, llm_service, tts_service
from app.services import pipeline_service
from app.services import voice_clone_service

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
    has_export = os.path.exists(export_service.get_export_path(video_id))

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
        has_export=has_export,
        export_url=f"/api/videos/{video_id}/export/download" if has_export else None,
        export_available=export_service.is_available() and config.EXPORT_ENABLED,
        separation_mode=config.SEPARATION_MODE,
        separation_backends=config.separation_capabilities(),
        enable_bgm_separation=state.enable_bgm_separation,
        enable_voice_clone=state.enable_voice_clone,
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

        # Prepare context payload. When the caller does not send end_time we
        # fall back to the persisted segment, so the length budget is still
        # available for single-line re-translation.
        stored = {seg.id: seg for seg in state.segments}
        context = [
            {
                "id": seg.id,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "start_time": seg.start_time,
                "end_time": seg.end_time
                or (stored[seg.id].end_time if seg.id in stored else 0.0),
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
    """Synthesize speech for a text segment using DashScope CosyVoice3."""
    logger.info(f"TTS requested for video_id: {video_id}")
    state = get_state(video_id)
    if not state:
        logger.warning(f"TTS failed: Video {video_id} not found")
        raise HTTPException(status_code=404, detail="Video not found")

    try:
        # Resolve voice: explicit request > cloned voice > speaker-assigned voice
        voice = req.voice
        if not voice:
            # Find which speaker this segment belongs to
            seg_obj = next((s for s in state.segments if s.id == req.segment_id), None)
            if seg_obj:
                cloned = voice_clone_service.get_cloned_voice(video_id, seg_obj.speaker_id)
                if cloned:
                    voice = cloned
                    logger.info(f"[{video_id}] Using cloned voice {voice} for segment {req.segment_id} (speaker {seg_obj.speaker_id})")

        await tts_service.synthesize_speech(
            video_id, req.segment_id, req.text, voice
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
                    separation_mode=req.separation_mode,
                    enable_bgm_separation=req.enable_bgm_separation,
                    enable_voice_clone=req.enable_voice_clone,
                    export_video=req.export_video,
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


@router.get("/{video_id}/voice-sample/{speaker_id}")
async def serve_voice_sample(video_id: str, speaker_id: str):
    """Serve extracted voice sample WAV for DashScope voice enrollment."""
    from fastapi.responses import FileResponse

    video_dir = get_video_dir(video_id)
    sample_path = os.path.join(video_dir, "voice_clone", speaker_id, "sample.wav")

    if not os.path.exists(sample_path):
        raise HTTPException(status_code=404, detail="Voice sample not found")

    return FileResponse(sample_path, media_type="audio/wav")


@router.get("/{video_id}/voice-clone/status")
async def get_voice_clone_status(video_id: str):
    """
    Cloned voices for a video, keyed by speaker: `{speaker_id: voice_id}`.

    Only voices enrolled against the currently configured TTS model are
    reported. Cloned voices cannot be used across models, so returning a stale
    one would make the UI show a speaker as "ready" while synthesis would
    actually fail with `InvalidParameter`.
    """
    video_dir = get_video_dir(video_id)
    current_model = voice_clone_service.clone_target_model()
    clone_map = voice_clone_service.load_clone_map(video_dir)

    usable = {
        speaker_id: entry["voice_id"]
        for speaker_id, entry in clone_map.items()
        if entry.get("voice_id") and entry.get("target_model") == current_model
    }

    return {
        "video_id": video_id,
        "cloned_voices": usable,
        "target_model": current_model,
    }


@router.post("/{video_id}/voice-clone/{speaker_id}/preview")
async def preview_cloned_voice(video_id: str, speaker_id: str):
    """Generate and serve a short TTS preview using the cloned voice."""
    from fastapi.responses import FileResponse

    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    # Determine target language from state or default
    target_language = "Chinese"  # default

    try:
        preview_path = await voice_clone_service.preview_cloned_voice(
            video_id, speaker_id, target_language
        )
        if not preview_path or not os.path.exists(preview_path):
            raise HTTPException(status_code=404, detail="No cloned voice available for this speaker")

        return FileResponse(preview_path, media_type="audio/mp3")
    except Exception as e:
        logger.error(f"[{video_id}] Voice preview failed for {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/voice-clone/{speaker_id}/preview")
async def serve_voice_preview(video_id: str, speaker_id: str):
    """Serve previously generated voice preview audio."""
    from fastapi.responses import FileResponse

    video_dir = get_video_dir(video_id)
    preview_path = os.path.join(video_dir, "voice_clone", speaker_id, "preview.mp3")

    if not os.path.exists(preview_path):
        raise HTTPException(status_code=404, detail="Voice preview not found. Generate one first via POST.")

    return FileResponse(preview_path, media_type="audio/mp3")


@router.post("/{video_id}/separator-token")
async def issue_separator_token(video_id: str, request: Request):
    """
    Issue a short-lived token authorising the client-compute endpoints for this
    video (`GET /api/models/separator/onnx`, `POST /api/videos/{id}/stems`).

    Requires a video that already exists, and issuance is rate limited per
    client address, so the heavy endpoints cannot be used as an open CDN.

    Note: this is an abuse speed bump, not authentication — there are no user
    accounts in this app.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    client_key = request.client.host if request.client else "unknown"
    if not token_service.check_issue_rate(client_key):
        raise HTTPException(
            status_code=429,
            detail="Too many token requests. Please retry later.",
        )

    token, ttl = token_service.issue_token(video_id)
    logger.info(f"[{video_id}] Issued separator token (ttl={ttl}s, client={client_key})")
    return {
        "video_id": video_id,
        "token": token,
        "expires_in": ttl,
        "model_download_url": "/api/models/separator/onnx",
    }


@router.post("/{video_id}/stems", dependencies=[Depends(enforce_separator_token)])
async def upload_stems(
    video_id: str,
    vocals: UploadFile = File(...),
    background: UploadFile = File(...),
):
    """
    Accept vocals/background stems produced by the **browser**.

    Separation normally runs client-side (see `app/routers/models.py`): the
    browser downloads the MDX-Net ONNX model, separates the locally decoded
    audio, and pushes the two WAV stems here. The rest of the pipeline then
    works exactly as if the server had done the separation — `vocals.wav` feeds
    ASR and voice cloning, `background.wav` is mixed back in during export.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    video_dir = get_video_dir(video_id)

    written = []
    for filename, upload in (("vocals.wav", vocals), ("background.wav", background)):
        dest = os.path.join(video_dir, filename)
        tmp = f"{dest}.part"
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(upload.file, out)

            size = os.path.getsize(tmp)
            if size < 44:  # smaller than a WAV header
                raise ValueError(f"{filename} is too small to be a WAV file ({size} bytes)")

            # Fail fast on non-WAV payloads instead of breaking ffmpeg later.
            with open(tmp, "rb") as f:
                header = f.read(4)
            if header[:4] not in (b"RIFF", b"RF64"):
                raise ValueError(f"{filename} is not a RIFF/WAV file (header={header!r})")

            os.replace(tmp, dest)
            written.append({"file": filename, "bytes": size})
            logger.info(f"[{video_id}] Received stem {filename} ({size / 1048576:.1f} MB)")
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            logger.error(f"[{video_id}] Failed to store stem {filename}: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid stem {filename}: {e}")

    # Separation is done — make sure the pipeline does not try to redo it.
    state.enable_bgm_separation = False
    state.audio_path = os.path.join(video_dir, "vocals.wav")
    save_state(state)

    return {
        "video_id": video_id,
        "stems": written,
        "background_url": f"/api/videos/{video_id}/audio/background",
    }


@router.post("/{video_id}/export", response_model=ExportResponse)
async def export_video(video_id: str):
    """
    Mux the dubbed audio back into the video and return the download URL.

    Cheap by design: the video stream is copied (`-c:v copy`), only the audio
    timeline is rebuilt, so this works even on a single-core CPU host.
    """
    logger.info(f"Export requested for video_id: {video_id}")

    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    if not config.EXPORT_ENABLED:
        raise HTTPException(status_code=503, detail="Export is disabled on this server")

    if not export_service.is_available():
        raise HTTPException(
            status_code=503, detail="ffmpeg/ffprobe are not available on the server"
        )

    try:
        output_path = await export_service.export_video(video_id)
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        return ExportResponse(
            video_id=video_id,
            url=f"/api/videos/{video_id}/export/download",
            size_mb=round(size_mb, 1),
        )
    except Exception as e:
        logger.error(f"[{video_id}] Export failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/export/download")
async def download_export(video_id: str):
    """Download the exported (dubbed) video."""
    from fastapi.responses import FileResponse

    export_path = export_service.get_export_path(video_id)
    if not os.path.exists(export_path):
        raise HTTPException(
            status_code=404, detail="Export not found. Run POST /export first."
        )

    return FileResponse(
        export_path,
        media_type="video/mp4",
        filename=f"translated_{video_id[:8]}.mp4",
    )
