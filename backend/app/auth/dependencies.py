"""FastAPI dependencies for authentication and project ownership."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.auth import sessions, users
from app.deps import resolve_token, resolve_video_id
from app.services import token_service


def current_user(request: Request) -> dict | None:
    token = sessions.request_session_token(request)
    return sessions.get_session(token)


def require_user(request: Request) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_project_access(
    request: Request,
    user: dict | None = Depends(current_user),
) -> dict:
    """404 (not 403) for another user's project, so IDs are not enumerable."""
    video_id = resolve_video_id(request)
    # External ASR/TTS/omni fetchers cannot carry a login cookie. They use a
    # short-lived signed capability token on the read-only audio endpoints.
    if request.method == "GET" and request.url.path.rstrip("/").endswith(("/audio", "/audio/original")):
        token = resolve_token(request)
        if token and token_service.verify_token(token, video_id):
            return {"id": None}
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    owner = users.owner_of(video_id)
    if owner is None or owner != user["id"]:
        raise HTTPException(status_code=404, detail="Project not found")
    return user
