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


# 只有这两条路由接受令牌代替登录。用路由模板精确匹配：按 URL 后缀判断的话，
# /tts/audio、/voice-sample/audio 这类"段 id 恰好叫 audio"的路径也会被放行。
_TOKEN_ROUTES = ("/projects/{video_id}/audio", "/projects/{video_id}/audio/original")


def _is_token_audio_route(request: Request) -> bool:
    route = request.scope.get("route")
    path = getattr(route, "path", "") or ""
    return path.rstrip("/").endswith(_TOKEN_ROUTES)


def require_project_access(
    request: Request,
    user: dict | None = Depends(current_user),
) -> dict:
    """404 (not 403) for another user's project, so IDs are not enumerable."""
    video_id = resolve_video_id(request)
    # External ASR/TTS/omni fetchers cannot carry a login cookie. They use a
    # short-lived signed capability token on the read-only audio endpoints.
    if request.method == "GET" and _is_token_audio_route(request):
        token = resolve_token(request)
        if token and token_service.verify_token(token, video_id):
            return {"id": None}
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    owner = users.owner_of(video_id)
    if owner is None or owner != user["id"]:
        raise HTTPException(status_code=404, detail="Project not found")
    return user
