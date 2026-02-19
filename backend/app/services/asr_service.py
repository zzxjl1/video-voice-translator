"""
Ali DashScope ASR service for speech-to-text transcription.
Uses the fun-asr model with speaker diarization.
"""
import logging
import hashlib
import json
import os
import subprocess
import time
import uuid
from typing import Optional

import requests

from app import config
from app.models import Segment

logger = logging.getLogger(__name__)


def extract_audio(video_path: str, audio_output_path: str) -> str:
    """Extract audio from video file using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",              # no video
        "-acodec", "pcm_s16le",
        "-ar", "16000",     # 16kHz sample rate for ASR
        "-ac", "1",         # mono
        audio_output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {result.stderr}")
    return audio_output_path


def compute_file_md5(file_path: str) -> str:
    """Compute MD5 hash of a file."""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


def submit_transcription_task(file_url: str) -> Optional[str]:
    """
    Submit a file transcription task to Ali DashScope ASR.
    Returns the task_id if successful, None otherwise.
    """
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    data = {
        "model": "fun-asr",
        "input": {"file_urls": [file_url]},
        "parameters": {
            "channel_id": [0],
            "diarization_enabled": True,
        },
    }
    service_url = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"

    response = requests.post(service_url, headers=headers, json=data)
    if response.status_code == 200:
        return response.json()["output"]["task_id"]
    else:
        raise RuntimeError(f"ASR task submission failed: {response.text}")


def poll_transcription_result(task_id: str, max_wait: int = 300) -> list[dict]:
    """
    Poll for transcription task completion.
    Returns list of result dicts when done.
    """
    headers = {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    service_url = f"https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

    start_time = time.time()
    while time.time() - start_time < max_wait:
        response = requests.get(service_url, headers=headers)
        if response.status_code == 200:
            output = response.json()["output"]
            status = output["task_status"]
            if status == "SUCCEEDED":
                return output.get("results", [])
            elif status in ("RUNNING", "PENDING"):
                time.sleep(1)
                continue
            else:
                error_msg = f"ASR task failed with status: {status}. Full response: {response.text}"
                logger.error(error_msg)
                raise RuntimeError(error_msg)
        else:
            error_msg = f"ASR task query failed with status {response.status_code}: {response.text}"
            logger.error(error_msg)
            raise RuntimeError(error_msg)

    raise TimeoutError("ASR task did not complete within timeout")


def parse_asr_results(results: list[dict]) -> list[Segment]:
    """
    Parse Ali ASR result JSON into Segment list.
    Each result contains a transcription_url that we need to fetch.
    """
    segments: list[Segment] = []

    for result in results:
        transcription_url = result.get("transcription_url")
        if not transcription_url:
            continue

        resp = requests.get(transcription_url)
        if resp.status_code != 200:
            continue

        data = resp.json()
        transcripts = data.get("transcripts", [])

        for transcript in transcripts:
            sentences = transcript.get("sentences", [])
            for sentence in sentences:
                seg_id = f"seg-{uuid.uuid4().hex[:8]}"
                speaker_id = sentence.get("spk_id", "0")
                segments.append(Segment(
                    id=seg_id,
                    speaker_label=f"Speaker {speaker_id}",
                    start_time=sentence.get("begin_time", 0) / 1000.0,  # ms -> seconds
                    end_time=sentence.get("end_time", 0) / 1000.0,
                    text=sentence.get("text", ""),
                ))

    return segments


async def transcribe_video(video_path: str, audio_path: str, file_serve_url: str) -> list[Segment]:
    """
    Full transcription pipeline:
    1. Extract audio from video
    2. Submit to Ali ASR
    3. Poll for results
    4. Parse into Segments
    """
    # Extract audio if not already done
    if not os.path.exists(audio_path):
        logger.info(f"--- [ASR] Step 1: Extracting audio to {audio_path} ---")
        extract_audio(video_path, audio_path)

    # Submit transcription task with the served file URL
    logger.info(f"--- [ASR] Step 2: Submitting task to Ali DashScope (URL: {file_serve_url}) ---")
    task_id = submit_transcription_task(file_serve_url)
    if not task_id:
        raise RuntimeError("Failed to submit transcription task")
    logger.info(f"--- [ASR] Step 2: Task submitted successfully. Task ID: {task_id} ---")

    # Poll for results
    logger.info(f"--- [ASR] Step 3: Polling for transcription results (Task: {task_id}) ---")
    results = poll_transcription_result(task_id)
    logger.info(f"--- [ASR] Step 3: Transcription completed successfully. ---")

    # Parse results into segments
    logger.info(f"--- [ASR] Step 4: Parsing ASR results into segments ---")
    segments = parse_asr_results(results)
    logger.info(f"--- [ASR] Step 4: Parsing complete. Generated {len(segments)} segments. ---")
    
    return segments
