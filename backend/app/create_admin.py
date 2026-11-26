"""Create an admin account (or promote an existing user to admin).

Admins cannot self-register through the API, so this is the one supported
way to get one:

    # local / venv
    python -m app.create_admin +919876543210 --name "Site Admin"

    # docker compose (from the backend/ folder)
    docker compose exec api python -m app.create_admin +919876543210 --name "Site Admin"

The password is prompted for (not echoed) unless --password is given.
"""
import argparse
import getpass
import sys

from app.auth import hash_password, normalize_phone
from app.database import SessionLocal, init_db, wait_for_db
from app.models import User, UserRole


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create or promote an Uzhavan.AI admin user")
    parser.add_argument("phone", help="phone number, e.g. +919876543210")
    parser.add_argument("--name", default="Admin", help="full name (only used when creating)")
    parser.add_argument("--password", help="password (prompted if omitted)")
    args = parser.parse_args(argv)

    phone = normalize_phone(args.phone)
    if not (11 <= len(phone) <= 14):
        print(f"'{args.phone}' does not look like a valid phone number", file=sys.stderr)
        return 2

    wait_for_db()
    init_db()  # no-op when migrations already created the tables
    with SessionLocal() as db:
        user = db.query(User).filter(User.phone_number.in_({phone, args.phone})).first()
        if user:
            user.role = UserRole.ADMIN
            db.commit()
            print(f"Promoted existing user {user.phone_number} to admin.")
            return 0

        password = args.password or getpass.getpass("Password (min 8 chars): ")
        if len(password) < 8:
            print("Password must be at least 8 characters.", file=sys.stderr)
            return 2
        db.add(User(
            full_name=args.name,
            phone_number=phone,
            hashed_password=hash_password(password),
            role=UserRole.ADMIN,
            preferred_language="en",
        ))
        db.commit()
        print(f"Created admin {phone}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
