"""
Model distribution routes.

The vocal-separation model runs **in the browser** (WebGPU / WASM), so the
server's only job is to hand the weights over once and describe how to drive
them. Serving the file ourselves — rather than shipping it in the frontend
bundle — keeps a 64 MB binary out of the JS build and out of git.
"""
import hashlib
import logging
import mimetypes
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app import config
from app.deps import enforce_separator_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["models"])

MODEL_MEDIA_TYPE = "application/octet-stream"


def separator_model_path() -> str:
    return os.path.join(config.MODELS_DIR, config.SEPARATOR_MODEL_FILE)


def _model_fingerprint(path: str) -> str:
    """Cheap content fingerprint (size + mtime) used for cache busting.

    Avoids hashing 64 MB on every metadata request while still changing when
    the file is replaced.
    """
    stat = os.stat(path)
    raw = f"{config.SEPARATOR_MODEL_FILE}:{stat.st_size}:{int(stat.st_mtime)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


@router.get("/separator")
async def separator_info():
    """
    Describe the client-side separation model.

    Returns the parameters the browser must use (they come straight from the
    server config, so the JS never hardcodes DSP constants) plus the download
    URL and size for progress reporting.
    """
    path = separator_model_path()
    if not os.path.exists(path):
        raise HTTPException(
            status_code=404,
            detail=(
                f"Separator model not found at {path}. Place "
                f"'{config.SEPARATOR_MODEL_FILE}' in backend/models/ or set "
                "SEPARATOR_MODEL_FILE."
            ),
        )

    size = os.path.getsize(path)
    return {
        "available": True,
        "mode": config.SEPARATION_MODE,
        "filename": config.SEPARATOR_MODEL_FILE,
        "size_bytes": size,
        "size_mb": round(size / 1024 / 1024, 1),
        "fingerprint": _model_fingerprint(path),
        "download_url": "/api/models/separator/onnx",
        "params": config.SEPARATOR_PARAMS,
        # Input tensor shape, handy for diagnostics on the client.
        "input_shape": [1, 4, config.SEPARATOR_PARAMS["dimF"], config.SEPARATOR_PARAMS["segmentSize"]],
    }


@router.api_route("/separator/onnx", methods=["GET", "HEAD"])
async def download_separator_model(request: Request):
    """
    Serve the ONNX weights with `Range` support so the browser can resume an
    interrupted 64 MB download and cache it aggressively (`immutable`).

    Gated by a short-lived token (see `app/services/token_service.py`) so the
    endpoint cannot be used as an open model CDN.
    """
    enforce_separator_token(request)

    path = separator_model_path()
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Separator model not found")

    file_size = os.path.getsize(path)
    fingerprint = _model_fingerprint(path)
    content_type = mimetypes.guess_type(path)[0] or MODEL_MEDIA_TYPE

    common_headers = {
        "Accept-Ranges": "bytes",
        # Versioned URL would be nicer, but the fingerprint in the ETag plus
        # the query string the client appends give the same effect.
        "ETag": f'"{fingerprint}"',
        "Cache-Control": "public, max-age=31536000, immutable",
    }

    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type=content_type,
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    range_header = request.headers.get("range")
    if not range_header:
        return FileResponse(path, media_type=content_type, headers=common_headers)

    try:
        range_spec = range_header.strip().split("=", 1)[1]
        start_str, _, end_str = range_spec.partition("-")
        start = int(start_str) if start_str else 0
        end = int(end_str) if end_str else file_size - 1
    except (IndexError, ValueError):
        raise HTTPException(status_code=416, detail="Malformed Range header")

    end = min(end, file_size - 1)
    if start > end or start >= file_size:
        raise HTTPException(status_code=416, detail="Range not satisfiable")

    content_length = end - start + 1

    def iter_range():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = dict(common_headers)
    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(content_length),
        }
    )
    return StreamingResponse(
        iter_range(), status_code=206, media_type=content_type, headers=headers
    )
