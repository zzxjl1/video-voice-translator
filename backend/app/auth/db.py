"""SQLite storage for users, identities, sessions, OAuth state and ownership.

Deliberately small and dependency-free. The project document itself remains a
JSON file on disk; this database only holds account/session state and the
project_id -> user_id mapping.
"""
from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              TEXT PRIMARY KEY,
    email           TEXT,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    name            TEXT,
    picture         TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS users_email_idx ON users(email);

CREATE TABLE IF NOT EXISTS identities (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    issuer          TEXT NOT NULL,
    subject         TEXT NOT NULL,
    email           TEXT,
    created_at      INTEGER NOT NULL,
    UNIQUE(provider, issuer, subject)
);
CREATE INDEX IF NOT EXISTS identities_user_idx ON identities(user_id);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    user_agent      TEXT,
    ip              TEXT,
    revoked_at      INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_idx ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash      TEXT PRIMARY KEY,
    browser_hash    TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    code_verifier   TEXT NOT NULL,
    redirect_uri    TEXT NOT NULL,
    return_to       TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    consumed_at     INTEGER
);

CREATE TABLE IF NOT EXISTS project_owners (
    project_id      TEXT PRIMARY KEY,
    owner_id        TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS project_owners_owner_idx ON project_owners(owner_id);
"""


def now() -> int:
    return int(time.time())


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.AUTH_DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(config.AUTH_DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def cleanup_expired() -> None:
    """Delete expired one-shot OAuth state and dead sessions."""
    t = now()
    with db() as conn:
        conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (t - 3600,))
        conn.execute(
            "DELETE FROM sessions WHERE expires_at < ? OR (revoked_at IS NOT NULL AND revoked_at < ?)",
            (t - 86400, t - 86400),
        )
