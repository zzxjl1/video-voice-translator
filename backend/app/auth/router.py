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


@router.get("/google/start")
def google_start(return_to: str = "/") -> RedirectResponse:
    if not config.AUTH_ENABLED:
        return _error_redirect("not_configured")

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

    # Rotate the browser session on login. Other devices keep their sessions.
    sessions.revoke_session(sessions.request_session_token(request))
    token = sessions.create_session(
        user_id=user["id"],
        user_agent=(request.headers.get("user-agent") or "")[:500],
        ip=request.client.host if request.client else None,
    )

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
    return response


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    sessions.revoke_session(sessions.request_session_token(request))
    response = JSONResponse({"ok": True})
    sessions.clear_session_cookie(response)
    return response
