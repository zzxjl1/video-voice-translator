"""User / identity upsert and project ownership mapping."""
from __future__ import annotations

import uuid

from app.auth import db
from app.auth.google import GoogleProfile

PROVIDER = "google"
ISSUER = "https://accounts.google.com"


class ProjectOwnedError(Exception):
    """The project already belongs to a different account."""


def _user_dict(row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "emailVerified": bool(row["email_verified"]),
        "name": row["name"],
        "picture": row["picture"],
    }


def upsert_google(profile: GoogleProfile) -> dict:
    """Create or refresh the local user for one Google subject."""
    now = db.now()
    with db.db() as conn:
        new_user = False
        identity = conn.execute(
            "SELECT user_id FROM identities WHERE provider = ? AND issuer = ? AND subject = ?",
            (PROVIDER, ISSUER, profile.subject),
        ).fetchone()
        if identity is not None:
            user_id = identity["user_id"]
            conn.execute(
                "UPDATE users SET email = ?, email_verified = ?, name = ?, picture = ?, updated_at = ? "
                "WHERE id = ?",
                (profile.email, 1, profile.name, profile.picture, now, user_id),
            )
            conn.execute(
                "UPDATE identities SET email = ? WHERE provider = ? AND issuer = ? AND subject = ?",
                (profile.email, PROVIDER, ISSUER, profile.subject),
            )
        else:
            user_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO users(id, email, email_verified, name, picture, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, profile.email, 1, profile.name, profile.picture, now, now),
            )
            conn.execute(
                "INSERT INTO identities(id, user_id, provider, issuer, subject, email, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex, user_id, PROVIDER, ISSUER, profile.subject, profile.email, now),
            )
            new_user = True
        row = conn.execute(
            "SELECT id, email, email_verified, name, picture FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()

    # 赠送放在 auth 事务之外：账本是另一个库，跨库不能共用事务；用固定主键保证只发一次。
    # 失败也不能让登录失败——送积分是附加动作。
    if new_user:
        try:
            from app.billing import service as billing
            billing.grant_signup_bonus(user_id)
        except Exception:  # noqa: BLE001
            logger.warning("signup bonus failed for %s", user_id, exc_info=True)

    return _user_dict(row)


def owner_of(project_id: str) -> str | None:
    with db.db() as conn:
        row = conn.execute(
            "SELECT owner_id FROM project_owners WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return row["owner_id"] if row else None


def assign_project(project_id: str, owner_id: str) -> None:
    """Map a project to a user; refuse to steal an existing mapping."""
    now = db.now()
    with db.db() as conn:
        row = conn.execute(
            "SELECT owner_id FROM project_owners WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is not None:
            if row["owner_id"] == owner_id:
                return
            raise ProjectOwnedError(project_id)
        conn.execute(
            "INSERT INTO project_owners(project_id, owner_id, created_at) VALUES (?, ?, ?)",
            (project_id, owner_id, now),
        )


def remove_project(project_id: str) -> None:
    with db.db() as conn:
        conn.execute("DELETE FROM project_owners WHERE project_id = ?", (project_id,))


def list_projects(owner_id: str) -> list[dict]:
    """Project ids owned by a user, newest assignment first."""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT project_id, created_at FROM project_owners "
            "WHERE owner_id = ? ORDER BY created_at DESC, project_id DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def release_project(project_id: str, owner_id: str) -> None:
    """Undo a claim made by this very request (upload failed after assign_project)."""
    with db.db() as conn:
        conn.execute(
            "DELETE FROM project_owners WHERE project_id = ? AND owner_id = ?",
            (project_id, owner_id),
        )
