"""
302.AI vocal separation backend (the "api" separation mode).

Contract, verified live
-----------------------
    POST {SEPARATION_API_BASE}/302/vt/subtitle/extract
      Authorization: Bearer <SEPARATION_API_KEY>
      {"audio_url": "<URL 302's servers can download>", "language": "en",
       "demucs": true, "is_only_demucs": true}
    -> 200 {"task_id": "2c436dd7-...-e1"}

    GET  {SEPARATION_API_BASE}/302/vt/tasks/subtitle/{task_id}
    -> 200 {"status": "queue" | "pending" | "processing" | "success" | "fail",
            "result": {...}, "progress": <int, optional>}

This endpoint is ASYNCHRONOUS. Note the query path is `vt/tasks/subtitle/{id}`
— "tasks" comes *before* "subtitle", the reverse of the submit path.

Pricing (from 302.AI's own spec): 0.001 PTC/min for separation only, 0.003
PTC/min for separation + transcription + alignment. Keeping
`is_only_demucs: true` is what selects the cheap mode.


Host selection — this is the part that silently breaks
------------------------------------------------------
302.AI publishes two base URLs:

    https://api.302.ai    — 海外. DNS-poisoned on mainland-China networks: it
                            resolves to Facebook-owned addresses (observed
                            66.220.148.145, then 31.13.95.33 — the fake IP
                            rotates, which is the signature of poisoning) and
                            never completes a TLS handshake.
    https://api.302ai.com — 国内环境2, documented and reachable.

`https://api.302ai.cn` also answers (Tencent Cloud, fastest) but is not in the
published spec, so it is not the default.

A task id is scoped to the environment that issued it — hence the `-e1` / `-e2`
suffix — so submit and poll must go to the same host.


What is NOT verified: the shape of `result`
-------------------------------------------
`result` for `is_only_demucs: true` is documented nowhere. The official
302.AI web app that uses this API has its polling code **commented out** and
never passes `is_only_demucs`, so no public code reveals the field names.

`_extract_output_urls()` therefore classifies the returned URLs by their key
names (`vocals`, `no_vocals`, `accompaniment`, …) and, failing that, falls back
to positional order — and raises with the full payload when it still cannot
tell them apart. The first real run will either work or log exactly what came
back, so this is a one-line fix rather than a guessing game.
"""
import asyncio
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Optional

import httpx

from app import config
from app.services.subtitle_service import probe_duration

logger = logging.getLogger(__name__)

# "no_vocals" contains "vocals", so background is always tested first.
_BACKGROUND_HINTS = (
    "no_vocal",
    "novocal",
    "no-vocal",
    "accompan",
    "instrumental",
    "background",
    "karaoke",
    "bgm",
    "music",
    "other",
    "inst",
    "伴奏",
)
_VOCALS_HINTS = ("vocal", "voice", "sing", "acapella", "人声")

_AUDIO_EXT_RE = re.compile(r"\.(wav|mp3|m4a|flac|ogg|opus|aac|mp4|mov|mkv)(\?|$)", re.I)


def _describe_api_error(response: httpx.Response) -> str:
    """Turn a 302.AI error envelope into a readable message."""
    try:
        body = response.json()
    except Exception:
        return f"302.AI returned HTTP {response.status_code}: {response.text[:400]}"

    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return (
            f"302.AI error {error.get('err_code')}: "
            f"{error.get('message_cn') or error.get('message')}"
        )
    return f"302.AI returned HTTP {response.status_code}: {str(body)[:400]}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.SEPARATION_API_KEY}",
        "Content-Type": "application/json",
    }


def _build_payload(video_id: str, server_url_base: str, language: str) -> dict:
    """
    Build the request body.

    302.AI fetches the audio itself, so `SERVER_URL_BASE` must be publicly
    reachable; a localhost or LAN-only value fails on their side with a download
    error. `is_only_demucs` selects the cheap separation-only mode.
    """
    return {
        "audio_url": f"{server_url_base.rstrip('/')}/api/videos/{video_id}/audio",
        "language": language,
        "demucs": True,
        "is_only_demucs": True,
    }


def _iter_urls(node, key_path: str = ""):
    """Yield (key_path, url) for every URL-looking string in a nested payload."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_urls(value, f"{key_path}.{key}" if key_path else str(key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _iter_urls(value, f"{key_path}[{index}]")
    elif isinstance(node, str) and node.startswith(("http://", "https://")):
        yield key_path, node


def _extract_output_urls(result: object) -> tuple[str, str]:
    """
    Pull (vocals_url, background_url) out of the task result.

    Names are unknown (see the module docstring), so this classifies by key
    hints and falls back to order, raising with the raw payload when it cannot
    decide. Failing loudly beats writing the wrong stem to disk.
    """
    pairs = [
        (key, url)
        for key, url in _iter_urls(result)
        if _AUDIO_EXT_RE.search(url) or "audio" in key.lower()
    ] or list(_iter_urls(result))

    if not pairs:
        raise RuntimeError(
            "302.AI reported success but the result contains no audio URLs. "
            f"Raw result: {str(result)[:800]}"
        )

    vocals: Optional[str] = None
    background: Optional[str] = None
    leftovers: list[str] = []

    for key, url in pairs:
        lowered = key.lower()
        if background is None and any(h in lowered for h in _BACKGROUND_HINTS):
            background = url
        elif vocals is None and any(h in lowered for h in _VOCALS_HINTS):
            vocals = url
        else:
            leftovers.append(url)

    if vocals and background:
        return vocals, background

    # Nothing recognisable: fall back to the order the server returned them in.
    if vocals is None and background is None and len(pairs) >= 2:
        logger.warning(
            "302.AI used unrecognised keys %s; falling back to positional order "
            "(first URL -> vocals, second -> background).",
            [key for key, _ in pairs],
        )
        return pairs[0][1], pairs[1][1]

    raise RuntimeError(
        "302.AI returned an unexpected stem layout "
        f"(vocals={vocals is not None}, background={background is not None}, "
        f"keys={[key for key, _ in pairs]}). Raw result: {str(result)[:800]}"
    )


async def _submit(client: httpx.AsyncClient, payload: dict) -> str:
    url = f"{config.SEPARATION_API_BASE}{config.SEPARATION_API_PATH}"
    response = await client.post(url, headers=_headers(), json=payload)
    if response.status_code >= 400:
        raise RuntimeError(_describe_api_error(response))

    task_id = (response.json() or {}).get("task_id")
    if not task_id:
        raise RuntimeError(
            f"302.AI accepted the request but returned no task_id: {response.text[:400]}"
        )
    return task_id


async def _poll(client: httpx.AsyncClient, task_id: str, emit) -> object:
    """Poll until the task succeeds, reporting progress through `emit`."""
    url = f"{config.SEPARATION_API_BASE}{config.SEPARATION_API_RESULT_PATH.format(task_id=task_id)}"
    deadline = time.monotonic() + config.SEPARATION_API_TIMEOUT
    last_status: Optional[str] = None
    last_progress: Optional[int] = None

    while time.monotonic() < deadline:
        response = await client.get(url, headers=_headers())
        if response.status_code >= 400:
            raise RuntimeError(_describe_api_error(response))

        body = response.json() or {}
        status = body.get("status")
        progress = body.get("progress")

        if status == "success":
            return body.get("result")
        if status == "fail":
            raise RuntimeError(f"302.AI task failed. Raw response: {str(body)[:500]}")

        if status != last_status or (progress is not None and progress != last_progress):
            logger.info(
                "[%s] 302.AI task status=%s progress=%s", task_id, status, progress
            )
            await emit(
                {
                    "phase": "separation",
                    "status": "processing",
                    "mode": "api",
                    "provider_status": status,
                    "progress": progress,
                }
            )
            last_status, last_progress = status, progress

        await asyncio.sleep(config.SEPARATION_API_POLL_INTERVAL)

    raise TimeoutError(
        f"302.AI separation timed out after {config.SEPARATION_API_TIMEOUT}s "
        f"(task {task_id}, last status {last_status})"
    )


async def separate_audio(
    video_id: str,
    audio_path: str,
    video_dir: str,
    server_url_base: str,
    emit,
    language: Optional[str] = None,
) -> dict:
    """
    Separate `audio_path` into vocals + background via 302.AI.

    Writes `vocals.wav` and `background.wav` into `video_dir` and returns their
    paths. Raises on any failure; the pipeline catches that and continues with
    the original mixed audio.
    """
    if not config.SEPARATION_API_KEY:
        raise RuntimeError("SEPARATION_API_KEY is not configured")

    payload = _build_payload(
        video_id, server_url_base, language or config.SEPARATION_API_LANGUAGE
    )
    logger.info("[%s] 302.AI separation -> audio_url=%s", video_id, payload["audio_url"])

    async with httpx.AsyncClient(timeout=config.SEPARATION_API_TIMEOUT) as client:
        task_id = await _submit(client, payload)
        logger.info("[%s] 302.AI task submitted: %s", video_id, task_id)

        result = await _poll(client, task_id, emit)
        vocals_url, background_url = _extract_output_urls(result)

        vocals_path = os.path.join(video_dir, "vocals.wav")
        background_path = os.path.join(video_dir, "background.wav")

        for stem_url, dest in ((vocals_url, vocals_path), (background_url, background_path)):
            stem = await client.get(stem_url)
            stem.raise_for_status()
            _write_as_wav(stem.content, dest)
            logger.info("[%s] 302.AI stem written: %s", video_id, dest)

    # 记账：302.AI 是按次/按时长收费的第三方调用，全流程只有这里知道它成功了。
    # 台账里【没有】它的价格规则，所以这条会以 cost_source="no_price" 落盘 ——
    # 这是刻意的：先让调用可见、数量可核，价格要按你们实际的计费口径再补。
    from app.services import usage_service
    _seconds = probe_duration(audio_path)
    usage_service.record(
        video_id=video_id,
        step="separation",
        model="302ai-vocal-separation",
        detail=task_id,
        usage={"audio_seconds": round(_seconds, 2)} if _seconds else {},
    )

    return {"vocals": vocals_path, "background": background_path}


def _write_as_wav(raw: bytes, dest: str) -> None:
    """Decode arbitrary audio bytes and store them as mono 24 kHz PCM WAV."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(raw)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", tmp_path,
                "-ac", "1", "-ar", "24000",
                "-acodec", "pcm_s16le", "-sample_fmt", "s16",
                dest,
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "ffmpeg failed to convert a separated stem: "
                f"{result.stderr.decode('utf-8', errors='replace')[-300:]}"
            )
    finally:
        os.unlink(tmp_path)
