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


def find_user(email: str) -> dict | None:
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, email, name FROM users WHERE lower(email) = lower(?) LIMIT 1",
            (email,),
        ).fetchone()
        return dict(row) if row else None


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

    user = find_user(args.email)
    if user is None:
        print(f"User not found: {args.email}", file=sys.stderr)
        return 1

    if args.project_id:
        ids = list(dict.fromkeys(args.project_id))
    elif args.all_anonymous:
        ids = all_project_ids()
    else:
        print("Pass --project-id or --all-anonymous", file=sys.stderr)
        return 2

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
