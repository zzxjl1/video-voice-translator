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
    """一次性消费：只有第一个到达的回调能拿到这一行。

    SELECT 再 UPDATE 不是原子的——两个并发回调都能先读到"未消费"，然后都通过。这里把"未消费、
    没过期、浏览器匹配"全部放进同一条 UPDATE 的 WHERE，用 rowcount 判断谁抢到了。
    """
    if not browser_token:
        return None
    t = db.now()
    state_hash = _hash(state)
    with db.db() as conn:
        cur = conn.execute(
            "UPDATE oauth_states SET consumed_at = ? "
            "WHERE state_hash = ? AND browser_hash = ? AND consumed_at IS NULL AND expires_at > ?",
            (t, state_hash, _hash(browser_token), t),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute(
            "SELECT nonce, code_verifier, redirect_uri, return_to FROM oauth_states WHERE state_hash = ?",
            (state_hash,),
        ).fetchone()
        return dict(row) if row else None
