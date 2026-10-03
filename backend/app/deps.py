"""
Shared FastAPI dependencies.

Currently just the capability-token gate for the endpoints that move real
bytes on behalf of the browser compute node.
"""
from fastapi import HTTPException, Request

from app import config
from app.services import token_service

TOKEN_HEADER = "x-separator-token"


def resolve_video_id(request: Request) -> str:
    """
    Find the video_id a request is about, from:
      1. the `X-Video-Id` header (explicit),
      2. a `video_id` query parameter (used by the model download, which the
         browser may issue as a bare GET/Range request), or
      3. the `video_id` path parameter (used by the stem upload).
    """
    return (
        request.headers.get("x-video-id")
        or request.query_params.get("video_id")
        or request.path_params.get("video_id")
        or ""
    ).strip()


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
