"""Create or attach a password credential using the test database only."""

import argparse
import getpass
import sys
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.schemas import is_valid_email
from app.auth.security import (
    PasswordPolicyError,
    hash_password,
    normalize_email,
    validate_password,
)
from app.models import User, UserCredential
from database import get_test_engine


DATABASE_NAME = "BFAMS-DeFI-test"


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.exit(2, "Invalid arguments.\n")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = SafeArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--attach-user-id", type=uuid.UUID)
    mode.add_argument("--new", action="store_true")
    parser.add_argument("--email", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def create_auth_user(
    session: Session,
    *,
    email: str,
    password: str,
    attach_user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    normalized_email = normalize_email(email)
    if not is_valid_email(normalized_email):
        raise ValueError("Invalid email.")
    try:
        validate_password(password, normalized_email)
    except PasswordPolicyError:
        raise

    if session.scalar(
        select(UserCredential.user_id).where(
            UserCredential.email == normalized_email
        )
    ) is not None:
        raise ValueError("Credential already exists.")

    if attach_user_id is None:
        user = User()
        session.add(user)
        session.flush()
        user_id = user.id
    else:
        user_id = attach_user_id
        if session.get(User, user_id) is None:
            raise ValueError("User does not exist.")
        if session.get(UserCredential, user_id) is not None:
            raise ValueError("Credential already exists.")

    credential = UserCredential(
        user_id=user_id,
        email=normalized_email,
        password_hash=hash_password(password, normalized_email),
        failed_attempts=0,
        locked_until=None,
        is_active=True,
        password_changed_at=datetime.now(timezone.utc),
    )
    session.add(credential)
    session.flush()
    return user_id


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise ValueError("Passwords do not match.")

    engine = get_test_engine()
    session = Session(engine)
    try:
        session.begin()
        database_name = session.execute(
            text("SELECT current_database()")
        ).scalar_one()
        if database_name != DATABASE_NAME:
            raise RuntimeError("Authentication requires BFAMS-DeFI-test.")
        user_id = create_auth_user(
            session,
            email=args.email,
            password=password,
            attach_user_id=args.attach_user_id,
        )
        if args.apply:
            session.commit()
        else:
            session.rollback()
        print(user_id)
        print("created")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


def main() -> int:
    try:
        return run()
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
