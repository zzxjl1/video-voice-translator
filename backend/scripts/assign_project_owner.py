#!/usr/bin/env python3
"""Assign existing project IDs to an already-registered account.

There is deliberately no runtime migration code for pre-auth projects. This
operator script is the one-off path:

    python scripts/assign_project_owner.py --project-id <id> --email user@example.com
    python scripts/assign_project_owner.py --all-anonymous --email user@example.com
    python scripts/assign_project_owner.py --all-anonymous --email user@example.com --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys

# Make `app` importable when this file is run directly from backend/.
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app import config  # noqa: E402
from app.auth import db, users  # noqa: E402
from app.deps import VIDEO_ID_RE  # noqa: E402


def find_user(email: str) -> dict | None:
    """按邮箱找账号。邮箱列不是 UNIQUE（用户可能改过邮箱）：匹配到多个就拒绝，不替你选一个。"""
    with db.db() as conn:
        rows = conn.execute(
            "SELECT id, email, name FROM users WHERE lower(email) = lower(?)",
            (email,),
        ).fetchall()
    if len(rows) > 1:
        print(f"{len(rows)} accounts share the email {email}; refusing to guess:", file=sys.stderr)
        for r in rows:
            print(f"  {r['id']}  {r['name']}", file=sys.stderr)
        sys.exit(3)
    return dict(rows[0]) if rows else None


def all_project_ids() -> list[str]:
    ids: list[str] = []
    for name in sorted(os.listdir(config.DATA_DIR)):
        path = os.path.join(config.DATA_DIR, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "project.json")):
            ids.append(name)
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help="an existing local user's email")
    parser.add_argument("--project-id", action="append", default=[], help="project/video id (repeatable)")
    parser.add_argument("--all-anonymous", action="store_true", help="assign every project with no owner")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db.init_db()                       # 库不存在时先建好，而不是直接抛 sqlite 错误
    user = find_user(args.email)
    if user is None:
        print(f"User not found: {args.email}", file=sys.stderr)
        return 1

    if args.project_id:
        ids = list(dict.fromkeys(args.project_id))
        bad = [i for i in ids if not VIDEO_ID_RE.fullmatch(i) or not os.path.isfile(os.path.join(config.DATA_DIR, i, "project.json"))]
        if bad:
            # 给不存在的 id 占位，会让之后别人上传同一个文件得到 409
            print(f"not an existing project id: {bad}", file=sys.stderr)
            return 2
    elif args.all_anonymous:
        ids = all_project_ids()
    else:
        print("Pass --project-id or --all-anonymous", file=sys.stderr)
        return 2

    if args.all_anonymous and not args.dry_run:
        n = sum(1 for i in ids if users.owner_of(i) is None)
        if input(f"Assign {n} ownerless projects to {user['email']}? [y/N] ").strip().lower() != "y":
            print("aborted")
            return 1

    assigned = skipped = conflicts = 0
    for project_id in ids:
        current = users.owner_of(project_id)
        if current == user["id"]:
            print(f"skip   {project_id} (already owned)")
            skipped += 1
            continue
        if current is not None:
            print(f"refuse {project_id} (owned by another user)")
            conflicts += 1
            continue
        if args.dry_run:
            print(f"would  {project_id} -> {user['email']}")
        else:
            users.assign_project(project_id, user["id"])
            print(f"assign {project_id} -> {user['email']}")
        assigned += 1

    print(f"\nuser: {user['email']} ({user['id']})")
    print(f"assigned: {assigned}, already-owned: {skipped}, conflicts: {conflicts}, dry-run: {args.dry_run}")
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
