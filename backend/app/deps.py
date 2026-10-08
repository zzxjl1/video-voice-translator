"""
Shared FastAPI dependencies.

Currently just the capability-token gate for the endpoints that move real
bytes on behalf of the browser compute node.
"""
import re

from fastapi import HTTPException, Request

from app import config
from app.services import token_service

TOKEN_HEADER = "x-separator-token"


def resolve_video_id(request: Request) -> str:
    """
    Find the video_id a request is about, from:
      1. the `video_id` **path parameter** — the工程 the request actually acts on,
      2. a `video_id` query parameter,
      3. the `X-Video-Id` header,
      4. the signed token itself.

    顺序很重要：令牌校验必须绑定到"这次真正要操作的那个工程"。以前头部排在
    路径参数前面，于是拿自己工程的 token、把头部填成自己的、路径写成别人的，
    校验通过而文件写进了别人的工程（P2 #15）。路径参数是唯一无法被"顺带带偏"
    的来源，所以它排第一。

    Case (4) exists because the model download is issued as
    `GET /api/models/separator/onnx?v=<fingerprint>&token=<token>`: the URL
    carries no video_id at all, so the binding has to be recovered from the
    token. Its payload is covered by the HMAC, so it cannot be forged, and
    `verify_token` still re-checks the signature, version, binding and expiry.
    """
    explicit = (
        request.path_params.get("video_id")
        or request.query_params.get("video_id")
        or request.headers.get("x-video-id")
        or ""
    ).strip()
    if explicit:
        return explicit

    return token_service.video_id_from_token(resolve_token(request)) or ""


# 工程 id 的形状：md5 十六进制，但客户端也可能带上下划线/短横线。长度上限是为了
# 挡住"用超长 id 建目录/写文件"。
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_video_id(request: Request) -> None:
    """
    路由级依赖：路径里的 video_id 必须是正常形状。

    以前完全没有校验，而 `get_video_dir` 会无条件 makedirs —— 任意一个
    `GET /api/videos/<任意字符串>/audio` 就能在磁盘上创建一个目录
    （P2 #16）。这里挡在入口：形状不对直接 400，不进入业务逻辑。
    """
    video_id = request.path_params.get("video_id")
    if video_id is not None and not _VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=400, detail="malformed video id")


def resolve_token(request: Request) -> str:
    """Token from the `X-Separator-Token` header, or a `token` query parameter."""
    return (
        request.headers.get(TOKEN_HEADER) or request.query_params.get("token") or ""
    ).strip()


def enforce_separator_token(request: Request) -> None:
    """
    Raise 403 unless the request carries a valid, unexpired token bound to the
    video it targets.

    Without this, `GET /api/models/separator/onnx` is an open 64 MB CDN and
    `POST /api/videos/{id}/stems` lets anyone write files into a video
    directory.
    """
    if not config.REQUIRE_SEPARATOR_TOKEN:
        return

    token = resolve_token(request)
    video_id = resolve_video_id(request)
    if not token or not video_id or not token_service.verify_token(token, video_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "A valid separator token is required. Issue one with "
                "POST /api/videos/{video_id}/separator-token."
            ),
        )
