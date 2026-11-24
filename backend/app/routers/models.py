"""
Model distribution routes.

The vocal-separation model runs **in the browser** (WebGPU / WASM), so the
server's only job is to hand the weights over once and describe how to drive
them. Serving the file ourselves — rather than shipping it in the frontend
bundle — keeps a 64 MB binary out of the JS build.
"""
import hashlib
import json
import logging
import mimetypes
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from app import config
from app.auth.dependencies import require_user
from app.deps import enforce_separator_token
from app.http_range import iter_file_range, parse_range

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
def separator_info():
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
                f"'{config.SEPARATOR_MODEL_FILE}' under backend/models/ (it ships with the "
                "repository) or set SEPARATOR_MODEL_FILE."
            ),
        )

    size = os.path.getsize(path)
    return {
        "available": True,
        "mode": config.SEPARATION_MODE,
        # Whether the UI should start with separation switched on. Kept here
        # rather than in /status so the client can initialise the picker before
        # any video exists.
        "default_enabled": config.ENABLE_BGM_SEPARATION_DEFAULT,
        # Per-backend availability + reason, so the picker never has to guess.
        "backends": config.separation_capabilities(),
        "filename": os.path.basename(config.SEPARATOR_MODEL_FILE),
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
        # The fingerprint in the ETag, plus the `?v=` the client appends, version
        # the URL. `private`, not `public`: the request carries a per-user token in
        # its query string, and a shared cache must not store or replay a response
        # for a URL that identifies a user's session.
        "ETag": f'"{fingerprint}"',
        "Cache-Control": "private, max-age=31536000, immutable",
    }

    if request.method == "HEAD":
        return Response(
            status_code=200,
            media_type=content_type,
            headers={**common_headers, "Content-Length": str(file_size)},
        )

    byte_range = parse_range(request.headers.get("range", ""), file_size)
    if byte_range is None:
        return FileResponse(path, media_type=content_type, headers=common_headers)

    start, end = byte_range
    content_length = end - start + 1
    headers = {
        **common_headers,
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Content-Length": str(content_length),
    }
    return StreamingResponse(
        iter_file_range(path, start, content_length, chunk=1024 * 1024),
        status_code=206,
        media_type=content_type,
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 浏览器本地配音（Kokoro）：模型 / 音色 / espeak 数据
# ---------------------------------------------------------------------------
#
# 这些文件原本由浏览器在运行时从 HuggingFace / jsDelivr 取：用户网络到不了就整个失败，也没有
# 任何完整性校验。现在它们随代码库提交（backend/models/kokoro-82m/，manifest 里有 sha256），
# 这里同源提供，运行时不访问第三方站点，部署也不需要下载。要登录：和工程接口一样，不当开放 CDN 用。

_KOKORO_EXT = {".onnx", ".json", ".bin", ".js", ".data"}
_KOKORO_EXTRA = {"COPYING"}          # espeak 的 GPL 许可文本，随文件一起提供


def _kokoro_manifest() -> dict | None:
    path = os.path.join(config.KOKORO_MODELS_DIR, "manifest.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)["files"]
    except (OSError, ValueError, KeyError):
        return None


@router.get("/kokoro", dependencies=[Depends(require_user)])
def kokoro_info():
    """本地配音引擎的资源是否齐全。缺了就明说怎么补，而不是让浏览器去撞 404。"""
    files = _kokoro_manifest()
    if files is None:
        return {"available": False, "missing": ["manifest.json"],
                "hint": "backend/models/kokoro-82m/ 应随代码库提交；缺失请恢复它（git checkout），或由维护者运行 scripts/prepare_kokoro_models.py 重新生成"}
    missing = [rel for rel, meta in files.items()
               if not os.path.exists(os.path.join(config.KOKORO_MODELS_DIR, rel))
               or os.path.getsize(os.path.join(config.KOKORO_MODELS_DIR, rel)) != meta["size"]]
    return {"available": not missing, "missing": missing,
            "size_mb": round(sum(m["size"] for m in files.values()) / 1e6),
            **({"hint": "这些文件应随代码库提交；请恢复（git checkout backend/models/kokoro-82m），或由维护者运行 scripts/prepare_kokoro_models.py 重新生成"} if missing else {})}


@router.api_route("/kokoro/{rel_path:path}", methods=["GET", "HEAD"], dependencies=[Depends(require_user)])
async def kokoro_file(rel_path: str, request: Request):
    """按相对路径提供 kokoro 目录下的文件（支持 Range，下载中断可续）。"""
    root = os.path.realpath(config.KOKORO_MODELS_DIR)
    path = os.path.realpath(os.path.join(root, rel_path))
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    # 路径穿越（.. / 符号链接）和非资源文件一律当不存在：不暴露 manifest 之外的东西
    if os.path.commonpath([root, path]) != root or not (ext in _KOKORO_EXT or name in _KOKORO_EXTRA):
        raise HTTPException(status_code=404, detail="Not found")
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail="本地配音资源缺失（应随代码库提交）；恢复 backend/models/kokoro-82m 或运行 scripts/prepare_kokoro_models.py",
        )

    file_size = os.path.getsize(path)
    manifest = _kokoro_manifest() or {}
    sha = (manifest.get(os.path.relpath(path, root)) or {}).get("sha256", "")
    content_type = mimetypes.guess_type(path)[0] or MODEL_MEDIA_TYPE
    headers = {
        "Accept-Ranges": "bytes",
        "ETag": f'"{sha[:16] or file_size}"',
        # 文件内容由 sha256 钉死，换文件要换路径或重新部署；private：请求带登录 cookie
        "Cache-Control": "private, max-age=2592000",
    }
    if request.method == "HEAD":
        return Response(status_code=200, media_type=content_type, headers={**headers, "Content-Length": str(file_size)})
    byte_range = parse_range(request.headers.get("range", ""), file_size)
    if byte_range is None:
        return FileResponse(path, media_type=content_type, headers=headers)
    start, end = byte_range
    length = end - start + 1
    return StreamingResponse(
        iter_file_range(path, start, length, chunk=1024 * 1024), status_code=206, media_type=content_type,
        headers={**headers, "Content-Range": f"bytes {start}-{end}/{file_size}", "Content-Length": str(length)},
    )
