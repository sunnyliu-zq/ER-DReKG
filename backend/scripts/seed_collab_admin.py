"""Seed the first collaboration-center admin member.

Idempotent: if the username already exists the script exits without changing
its password or role. Run from the backend directory:

    uv run python scripts/seed_collab_admin.py --username admin --display-name 管理员

The password can be passed via --password or the COLLAB_ADMIN_PASSWORD
environment variable; otherwise it is prompted interactively.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.collab_auth import hash_password  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import CollabMember  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed a collab-center admin member")
    parser.add_argument("--username", required=True, help="Login name (3-64 chars)")
    parser.add_argument("--display-name", required=True, help="Display name")
    parser.add_argument("--password", default="", help="Password (or use COLLAB_ADMIN_PASSWORD)")
    parser.add_argument("--role", default="admin", choices=["admin", "member"])
    args = parser.parse_args()

    username = args.username.strip().lower()
    if not (3 <= len(username) <= 64):
        raise SystemExit("username must be 3-64 characters")

    password = args.password or os.environ.get("COLLAB_ADMIN_PASSWORD", "")
    if not password:
        password = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            raise SystemExit("Passwords do not match")
    if not (1 <= len(password) <= 128):
        raise SystemExit("password must be 1-128 characters")

    with SessionLocal() as db:
        existing = db.scalar(select(CollabMember).where(CollabMember.username == username))
        if existing is not None:
            print(f"Member '{username}' already exists; no changes made.")
            return
        member = CollabMember(
            username=username,
            password_hash=hash_password(password),
            display_name=args.display_name,
            role=args.role,
            is_active=True,
        )
        db.add(member)
        db.commit()
        print(f"Created {args.role} '{username}' ({member.member_id}).")


if __name__ == "__main__":
    main()
