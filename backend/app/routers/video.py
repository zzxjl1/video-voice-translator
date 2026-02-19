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
)
from app.services import asr_service, llm_service, tts_service

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
    """Upload a video file. Returns video_id (MD5 hash)."""
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

        if not os.path.exists(final_path):
            os.rename(temp_path, final_path)
            logger.info(f"Saved new video file: {final_path}")
        else:
            os.remove(temp_path)  # Already uploaded before
            logger.info(f"Video already exists, skipping save: {final_path}")

        # Create/retrieve state
        state = get_or_create_state(video_id, file.filename, final_path)
        save_state(state)

        return UploadResponse(video_id=video_id, filename=file.filename)
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/status", response_model=VideoStatusResponse)
async def get_video_status(video_id: str):
    """Get the current processing status of a video."""
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

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
    )


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
        audio_base64, content_type = await tts_service.synthesize_speech(
            video_id, req.segment_id, req.text, req.voice
        )
        logger.info(f"[{video_id}] TTS synthesis complete (Segment: {req.segment_id})")

        # Update in-memory state to reflect the new audio path
        for seg in state.segments:
            if seg.id == req.segment_id:
                seg.audio_path = os.path.join(get_video_dir(video_id), "tts", f"{req.segment_id}.mp3")
                break

        return TTSResponse(
            audio_base64=audio_base64,
            content_type=content_type,
        )
    except Exception as e:
        logger.error(f"[{video_id}] TTS error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/video")
async def serve_video(video_id: str):
    """Serve the original video file."""
    from fastapi.responses import FileResponse

    state = get_state(video_id)
    if not state or not state.file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    if not os.path.exists(state.file_path):
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    return FileResponse(state.file_path, media_type="video/mp4")


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


@router.get("/{video_id}/tts/{segment_id}")
async def serve_segment_audio(video_id: str, segment_id: str):
    """Serve synthesized MP3 for a specific segment."""
    from fastapi.responses import FileResponse
    video_dir = get_video_dir(video_id)
    audio_path = os.path.join(video_dir, "tts", f"{segment_id}.mp3")
    
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Segment audio not found")
        
    return FileResponse(audio_path, media_type="audio/mp3")
