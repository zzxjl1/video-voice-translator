"""Google OAuth login, current-user and logout endpoints."""
from __future__ import annotations

import logging
import secrets
from urllib.parse import quote

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app import config
from app.auth import google, sessions, states, users
from app.auth.dependencies import current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _safe_return_to(value: str | None) -> str:
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def _error_redirect(code: str) -> RedirectResponse:
    return RedirectResponse(url=f"/?auth_error={quote(code, safe='')}", status_code=302)


def _clear_oauth_cookie(response: Response) -> None:
    response.delete_cookie(
        key=config.OAUTH_BROWSER_COOKIE,
        path="/",
        secure=config.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def _client_ip(request: Request) -> str | None:
    """反代后 request.client.host 恒为 127.0.0.1，真实地址在 X-Real-IP（只在来自本机的请求里信它）。"""
    peer = request.client.host if request.client else None
    if peer in ("127.0.0.1", "::1"):
        return (request.headers.get("x-real-ip") or "").strip() or peer
    return peer


@router.get("/google/start")
def google_start(request: Request, return_to: str = "/") -> RedirectResponse:
    if not config.AUTH_ENABLED:
        return _error_redirect("not_configured")
    # 这个接口未登录就能调，每次都会往 oauth_states 写一行：不限流就能被拿来刷库。
    # 复用令牌签发的滑动窗口限流（按来源地址）。
    from app.services import token_service
    if not token_service.check_issue_rate(f"oauth:{_client_ip(request) or 'unknown'}"):
        return _error_redirect("rate_limited")

    state = secrets.token_urlsafe(32)
    browser_token = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(24)
    verifier, challenge = google.pkce_pair()
    target = _safe_return_to(return_to)

    states.create_state(
        state=state,
        browser_token=browser_token,
        nonce=nonce,
        code_verifier=verifier,
        redirect_uri=config.GOOGLE_REDIRECT_URI,
        return_to=target,
    )

    response = RedirectResponse(
        url=google.authorization_url(
            state=state,
            nonce=nonce,
            code_challenge=challenge,
            redirect_uri=config.GOOGLE_REDIRECT_URI,
        ),
        status_code=302,
    )
    response.set_cookie(
        key=config.OAUTH_BROWSER_COOKIE,
        value=browser_token,
        max_age=config.OAUTH_STATE_TTL_SECONDS,
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    if error:
        response = _error_redirect("access_denied")
        _clear_oauth_cookie(response)
        return response
    if not code or not state:
        response = _error_redirect("invalid_state")
        _clear_oauth_cookie(response)
        return response

    browser_token = request.cookies.get(config.OAUTH_BROWSER_COOKIE)
    pending = states.consume_state(state, browser_token)
    if pending is None:
        response = _error_redirect("invalid_state")
        _clear_oauth_cookie(response)
        return response

    try:
        token_data = google.exchange_code(
            code=code,
            code_verifier=pending["code_verifier"],
            redirect_uri=pending["redirect_uri"],
        )
        profile = google.verify_id_token(token_data["id_token"], pending["nonce"])
        user = users.upsert_google(profile)
    except google.OAuthError as e:
        logger.warning("Google login failed: %s", e)
        response = _error_redirect("login_failed")
        _clear_oauth_cookie(response)
        return response
    except Exception:  # noqa: BLE001 - 数据库 / 网络 / 对方返回了意外的形状：回登录页，不要给用户一个裸 500
        logger.exception("Google login crashed")
        response = _error_redirect("login_failed")
        _clear_oauth_cookie(response)
        return response

    # Rotate the browser session on login. Other devices keep their sessions.
    try:
        sessions.revoke_session(sessions.request_session_token(request))
        token = sessions.create_session(
            user_id=user["id"],
            user_agent=(request.headers.get("user-agent") or "")[:500],
            ip=_client_ip(request),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Creating session failed")
        response = _error_redirect("login_failed")
        _clear_oauth_cookie(response)
        return response

    response = RedirectResponse(
        url=_safe_return_to(pending["return_to"]),
        status_code=302,
    )
    sessions.set_session_cookie(response, token)
    _clear_oauth_cookie(response)
    return response


@router.get("/me")
def me(request: Request) -> JSONResponse:
    user = current_user(request)
    response = JSONResponse({"user": user})
    response.headers["Cache-Control"] = "no-store"
    # 服务端每小时把会话往后推 30 天，但 cookie 只在登录时设置过一次：不重发的话，浏览器里的
    # cookie 到登录后第 30 天照样过期，活跃用户也被登出。前端每次加载都会调 /me，在这里重发。
    token = sessions.request_session_token(request)
    if user is not None and token:
        sessions.set_session_cookie(response, token)
    return response


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    sessions.revoke_session(sessions.request_session_token(request))
    response = JSONResponse({"ok": True})
    sessions.clear_session_cookie(response)
    return response
