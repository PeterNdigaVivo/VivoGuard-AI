"""CLI: reset (or create) a user's password using the current direct-bcrypt
hashing path.

Run inside the api container:

    docker compose exec api python -m app.scripts.reset_admin \\
        --email admin@example.com --password 'NewStrong#Pass1' [--role admin]

If the user doesn't exist it is created. If it does, the password_hash is
rewritten and is_active forced to True. Safe to re-run.
"""
from __future__ import annotations
import argparse
import sys

from app.auth.security import hash_password
from app.database import SessionLocal
from app.models import User


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--email",    required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--role",     default="admin",
                   choices=["admin", "operator", "viewer"])
    p.add_argument("--full-name", default=None)
    args = p.parse_args(argv)

    with SessionLocal() as db:
        u = db.query(User).filter(User.email == args.email).first()
        if u is None:
            u = User(email=args.email, full_name=args.full_name or args.email,
                     role=args.role, is_active=True,
                     password_hash=hash_password(args.password))
            db.add(u)
            db.commit()
            print(f"created user {args.email} ({args.role})")
            return 0
        u.password_hash = hash_password(args.password)
        u.is_active = True
        u.role = args.role
        if args.full_name:
            u.full_name = args.full_name
        db.commit()
        print(f"reset password for {args.email} (role={args.role}, active=True)")
        return 0


if __name__ == "__main__":
    sys.exit(main())
