"""Short-lived, one-shot OAuth state rows."""
from __future__ import annotations

import hashlib

from app import config
from app.auth import db


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_state(
    state: str,
    browser_token: str,
    nonce: str,
    code_verifier: str,
    redirect_uri: str,
    return_to: str,
) -> None:
    t = db.now()
    with db.db() as conn:
        # Opportunistic cleanup keeps this tiny table from growing forever.
        conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (t - 3600,))
        conn.execute(
            "INSERT INTO oauth_states(state_hash, browser_hash, nonce, code_verifier, "
            "redirect_uri, return_to, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _hash(state), _hash(browser_token), nonce, code_verifier,
                redirect_uri, return_to, t, t + config.OAUTH_STATE_TTL_SECONDS,
            ),
        )


def consume_state(state: str, browser_token: str | None) -> dict | None:
    if not browser_token:
        return None
    t = db.now()
    with db.db() as conn:
        row = conn.execute(
            "SELECT state_hash, nonce, code_verifier, redirect_uri, return_to "
            "FROM oauth_states WHERE state_hash = ? AND consumed_at IS NULL AND expires_at > ?",
            (_hash(state), t),
        ).fetchone()
        if row is None:
            return None
        expected_browser = conn.execute(
            "SELECT browser_hash FROM oauth_states WHERE state_hash = ?",
            (_hash(state),),
        ).fetchone()
        if expected_browser is None or expected_browser["browser_hash"] != _hash(browser_token):
            return None
        conn.execute(
            "UPDATE oauth_states SET consumed_at = ? WHERE state_hash = ?",
            (t, _hash(state)),
        )
        return dict(row)
