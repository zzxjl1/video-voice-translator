"""
Ali DashScope ASR service for speech-to-text transcription.

The default model is `fun-asr` and speaker diarization is always requested
(see `config.ASR_MODEL`); a model that cannot do it degrades by plan, not by
a config flag.

Diarization support is provider-side, so it is validated here:

* The model name is checked against `config.ASR_DIARIZATION_MODELS`, a
  whitelist verified against the Alibaba Cloud Model Studio docs — as of the
  docs' latest revision `diarization_enabled` is supported by **Fun-ASR,
  the Paraformer series and Qwen-Audio-3.x-ASR-Flash-Filetrans**, and is
  *not* documented for Qwen3-ASR-Flash.
* If submission or the task itself fails, the request is retried
  progressively (same model without diarization, then the fallback model),
  so a mis-configured model name degrades instead of hard-failing.
* Diarization also requires MONO audio and works best under ~2 hours; the
  extracted audio is already 16 kHz mono.

Keep the model here rather than replacing it blindly: self-hosting ASR +
diarization is not viable on a small CPU-only host, so this cloud call is
the cheapest way to get transcript + timestamps + speaker ids at once.
"""
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from typing import Optional

import httpx

from app import config
from app.services import usage_service
# 复用时长的探测（ffprobe）：subtitle_service 只依赖 config，不会成环。
from app.services.subtitle_service import probe_duration
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


def _asr_headers() -> dict:
    return {
        "Authorization": f"Bearer {config.DASHSCOPE_API_KEY}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }


def check_diarization_support(model: str) -> bool:
    """Is `diarization_enabled` documented as supported for this model?"""
    return model in config.ASR_DIARIZATION_MODELS


async def submit_transcription_task(
    file_url: str,
    model: Optional[str] = None,
    diarization: Optional[bool] = None,
) -> str:
    """
    Submit a file transcription task to Ali DashScope ASR.
    Returns the task_id.
    """
    model = model or config.ASR_MODEL
    diarization = True if diarization is None else diarization

    parameters: dict = {"channel_id": [0]}
    if diarization:
        if not check_diarization_support(model):
            # Fail fast with a clear message instead of letting the provider
            # silently ignore the flag (which would collapse every line to a
            # single speaker without any error).
            raise RuntimeError(
                f"Model '{model}' is not in the known diarization-capable list "
                f"({sorted(config.ASR_DIARIZATION_MODELS)}). The attempt plan "
                "falls through to the same model without diarization, then to the "
                f"fallback model ({config.ASR_FALLBACK_MODEL}) with it."
            )
        parameters["diarization_enabled"] = True

    data = {
        "model": model,
        "input": {"file_urls": [file_url]},
        "parameters": parameters,
    }

    async with httpx.AsyncClient(timeout=config.API_GENERAL_TIMEOUT) as client:
        response = await client.post(
            config.ASR_SERVICE_URL, headers=_asr_headers(), json=data
        )
        if response.status_code == 200:
            return response.json()["output"]["task_id"]
        raise RuntimeError(f"ASR task submission failed: {response.text}")


async def poll_transcription_result(
    task_id: str, max_wait: int = None
) -> tuple[list[dict], dict]:
    """
    Poll for transcription task completion.
    Returns (result dicts, usage) when done. `usage` is whatever the service
    reports (empty dict when it reports nothing) — the ledger keeps it verbatim.
    """
    if max_wait is None:
        max_wait = config.ASR_MAX_WAIT

    service_url = config.ASR_TASK_URL_TEMPLATE.format(task_id=task_id)

    start_time = time.time()
    async with httpx.AsyncClient(timeout=config.API_GENERAL_TIMEOUT) as client:
        while time.time() - start_time < max_wait:
            response = await client.get(service_url, headers=_asr_headers())
            if response.status_code == 200:
                output = response.json()["output"]
                status = output["task_status"]
                if status == "SUCCEEDED":
                    return output.get("results", []), dict(output.get("usage") or {})
                elif status in ("RUNNING", "PENDING"):
                    await asyncio.sleep(config.ASR_POLL_INTERVAL)
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


async def parse_asr_results(results: list[dict]) -> list[Segment]:
    """
    Parse Ali ASR result JSON into Segment list.
    Each result contains a transcription_url that we need to fetch.
    """
    segments: list[Segment] = []

    async with httpx.AsyncClient(timeout=config.API_GENERAL_TIMEOUT) as client:
        for result in results:
            transcription_url = result.get("transcription_url")
            if not transcription_url:
                continue

            resp = await client.get(transcription_url)
            if resp.status_code != 200:
                logger.error(f"Failed to fetch transcription result from {transcription_url}")
                continue

            data = resp.json()
            transcripts = data.get("transcripts", [])

            for transcript in transcripts:
                sentences = transcript.get("sentences", [])
                for sentence in sentences:
                    seg_id = f"seg-{uuid.uuid4().hex[:8]}"
                    speaker_id = str(sentence.get("speaker_id", "0"))
                    segments.append(Segment(
                        id=seg_id,
                        speaker_id=speaker_id,
                        speaker_label=f"Speaker {speaker_id}",
                        start_time=sentence.get("begin_time", 0) / 1000.0,  # ms -> seconds
                        end_time=sentence.get("end_time", 0) / 1000.0,
                        text=sentence.get("text", ""),
                    ))

    return segments


from app.models import Segment, get_video_dir


def _build_attempt_plan() -> list[tuple[str, bool]]:
    """
    Ordered list of (model, diarization) attempts.

    1. Configured model with diarization.
    2. Same model without it (the provider may reject the flag).
    3. The fallback model with diarization.

    Diarization is always the goal; only the provider's support decides how far
    down this list a run has to go. Duplicates are removed, order preserved.
    """
    plan: list[tuple[str, bool]] = [(config.ASR_MODEL, True), (config.ASR_MODEL, False)]
    if config.ASR_FALLBACK_MODEL != config.ASR_MODEL:
        plan.append((config.ASR_FALLBACK_MODEL, True))

    seen: set[tuple[str, bool]] = set()
    unique: list[tuple[str, bool]] = []
    for entry in plan:
        if entry not in seen:
            seen.add(entry)
            unique.append(entry)
    return unique


async def _run_transcription(
    file_serve_url: str, model: str, diarization: bool
) -> tuple[list[dict], dict]:
    """提交 + 轮询。返回 (results, usage) —— usage 原样来自服务端（可能为空）。"""
    logger.info(
        f"--- [ASR] Submitting task (model={model}, diarization={diarization}) ---"
    )
    task_id = await submit_transcription_task(file_serve_url, model, diarization)
    logger.info(f"--- [ASR] Task submitted. Task ID: {task_id} ---")

    logger.info(f"--- [ASR] Polling for transcription results (Task: {task_id}) ---")
    results, usage = await poll_transcription_result(task_id)
    logger.info("--- [ASR] Transcription completed. ---")
    return results, usage


async def transcribe_video(video_id: str, video_path: str, audio_path: str, file_serve_url: str) -> list[Segment]:
    """
    Full transcription pipeline:
    1. Extract audio from video
    2. Submit to Ali ASR (with graceful degradation if diarization fails)
    3. Poll for results
    4. Parse into Segments
    5. Save result to disk
    """
    # Extract audio if not already done
    if not os.path.exists(audio_path):
        logger.info(f"--- [ASR] Step 1: Extracting audio to {audio_path} ---")
        extract_audio(video_path, audio_path)

    # Resolve which model actually can do diarization before spending a call.
    plan = _build_attempt_plan()
    if not check_diarization_support(config.ASR_MODEL):
        logger.warning(
            f"[ASR] Configured model '{config.ASR_MODEL}' is not in the verified "
            f"diarization whitelist. Speaker separation may be lost. "
            f"Fallback plan: {plan}"
        )

    # Step 2+3: submit + poll, degrading on failure
    results: Optional[list[dict]] = None
    last_error: Optional[Exception] = None
    api_usage: dict = {}
    for attempt, (model, diarization) in enumerate(plan, start=1):
        try:
            results, api_usage = await _run_transcription(
                file_serve_url, model, diarization
            )
            if not diarization:
                logger.warning(
                    "[ASR] Diarization is OFF for this run — every segment will be "
                    "attributed to a single speaker."
                )
            break
        except Exception as e:  # noqa: BLE001 - we retry with a degraded config
            last_error = e
            logger.warning(
                f"[ASR] Attempt {attempt}/{len(plan)} (model={model}, "
                f"diarization={diarization}) failed: {e}"
            )

    if results is None:
        raise RuntimeError(f"ASR failed on all attempts: {last_error}")

    # 记账：这是全流程里【唯一】知道"ASR 成功了、用的哪个模型、送了多少音频"的
    # 位置。以前整条链路没有一处记账，台账里看不到 ASR 的费用 —— 价格表里那条
    # ("asr", "qwen-audio-3.1-asr-flash-filetrans") 规则因此从未被使用过。
    # 用量优先用服务端回传的（实测 2026-10-08：根层与 output.usage 都带
    # {"duration": 26, "input_tokens": 495, "output_tokens": 119}，正好对上价格表
    # 的 token 口径）。只有服务端没给时长时才用本地 ffprobe 兜底。
    _usage: dict = dict(api_usage)
    if not _usage.get("duration"):
        _seconds = probe_duration(audio_path)
        if _seconds:
            _usage["audio_seconds"] = round(_seconds, 2)
    usage_service.record(
        video_id=video_id,
        step="asr",
        model=model,
        detail=f"diarization={'on' if diarization else 'off'}",
        usage=_usage,
    )

    # Parse results into segments
    logger.info(f"--- [ASR] Step 4: Parsing ASR results into segments ---")
    segments = await parse_asr_results(results)
    logger.info(f"--- [ASR] Step 4: Parsing complete. Generated {len(segments)} segments. ---")
    
    # Save PROCESSED segments to disk (for easy recovery, ASR ONLY)
    video_dir = get_video_dir(video_id)
    result_path = os.path.join(video_dir, "asr_result.json")
    try:
        # Use explicit dict literal to include only ASR-relevant fields
        asr_only_segments = [
            {
                "id": s.id,
                "speaker_id": s.speaker_id,
                "speaker_label": s.speaker_label,
                "start_time": s.start_time,
                "end_time": s.end_time,
                "text": s.text,
            }
            for s in segments
        ]
            
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(asr_only_segments, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved processed ASR segments to {result_path}")
    except Exception as e:
        logger.warning(f"Failed to save processed ASR segments: {e}")
        
    return segments
