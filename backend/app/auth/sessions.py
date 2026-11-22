"""Opaque server-side sessions and the browser cookie that carries them."""
from __future__ import annotations

import hashlib
import secrets
import time

from fastapi import Request, Response

from app import config
from app.auth import db

SESSION_COOKIE = config.SESSION_COOKIE_NAME


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "emailVerified": bool(row["email_verified"]),
        "name": row["name"],
        "picture": row["picture"],
    }


def create_session(user_id: str, user_agent: str | None, ip: str | None) -> str:
    token = secrets.token_urlsafe(32)
    t = db.now()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen_at, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_hash(token), user_id, t, t + config.SESSION_DAYS * 86400, t, user_agent, ip),
        )
    return token


def get_session(raw_token: str | None) -> dict | None:
    """Look up a live session; refresh the sliding expiry at most hourly."""
    if not raw_token:
        return None
    token_hash = _hash(raw_token)
    t = db.now()
    with db.db() as conn:
        row = conn.execute(
            "SELECT s.token_hash, s.user_id, s.expires_at, s.last_seen_at, "
            "       u.id, u.email, u.email_verified, u.name, u.picture, u.status "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ?",
            (token_hash, t),
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        if t - row["last_seen_at"] >= config.SESSION_TOUCH_SECONDS:
            conn.execute(
                "UPDATE sessions SET last_seen_at = ?, expires_at = ? WHERE token_hash = ?",
                (t, t + config.SESSION_DAYS * 86400, token_hash),
            )
        return _public_user(row)


def revoke_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    with db.db() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (db.now(), _hash(raw_token)),
        )


def revoke_user_sessions(user_id: str) -> None:
    with db.db() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (db.now(), user_id),
        )


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        secure=config.SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE,
        path="/",
        secure=config.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def request_session_token(request: Request) -> str | None:
    return request.cookies.get(SESSION_COOKIE)
