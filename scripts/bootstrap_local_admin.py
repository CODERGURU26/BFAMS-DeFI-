"""Create the local BFAMS bootstrap login in the TEST database."""

import argparse
import getpass
import sys

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.security import PasswordPolicyError, validate_password
from app.auth.schemas import is_valid_email
from app.models import (
    AuthEvent,
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    UserCredential,
)
from database import get_test_engine
from scripts.create_auth_user import create_auth_user


DATABASE_NAME = "BFAMS-DeFI-test"
BOOTSTRAP_EMAIL = "admin@bfams.local"


def run(apply: bool = False) -> int:
    engine = get_test_engine()
    session = Session(engine)
    try:
        session.begin()
        database_name = session.execute(
            text("SELECT current_database()")
        ).scalar_one()
        if database_name != DATABASE_NAME:
            raise RuntimeError("Bootstrap requires BFAMS-DeFI-test.")

        dhan_owner_ids = session.scalars(
            select(FinancialConnection.user_id)
            .join(
                DataProvider,
                DataProvider.id == FinancialConnection.provider_id,
            )
            .join(
                FinancialAccount,
                FinancialAccount.connection_id == FinancialConnection.id,
            )
            .where(
                DataProvider.provider_code == "dhan_manual",
                FinancialAccount.external_account_id == "DHAN-DEMAT-1",
            )
            .distinct()
        ).all()
        if len(dhan_owner_ids) != 1:
            raise RuntimeError(
                "Expected exactly one TEST Dhan account owner; no changes made."
            )
        dhan_owner_id = dhan_owner_ids[0]

        existing = session.scalar(
            select(UserCredential).where(
                UserCredential.email == BOOTSTRAP_EMAIL
            )
        )
        if existing is not None:
            if existing.user_id != dhan_owner_id:
                raise RuntimeError(
                    "Bootstrap email belongs to a different TEST user."
                )
            session.rollback()
            print("Bootstrap account already exists; unchanged.")
            return 0
        if session.get(UserCredential, dhan_owner_id) is not None:
            raise RuntimeError(
                "The Dhan owner already has a login credential; no changes made."
            )

        if not is_valid_email(BOOTSTRAP_EMAIL):
            raise ValueError("Invalid bootstrap email.")

        if not apply:
            session.rollback()
            print("Dry run: bootstrap login would be attached to the Dhan owner.")
            return 0

        password = getpass.getpass("Bootstrap password: ")
        confirmation = getpass.getpass("Confirm bootstrap password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match.")
        try:
            validate_password(password, BOOTSTRAP_EMAIL)
        except PasswordPolicyError:
            raise ValueError("Password does not meet authentication policy.") from None

        user_id = create_auth_user(
            session,
            email=BOOTSTRAP_EMAIL,
            password=password,
            attach_user_id=dhan_owner_id,
        )
        session.add(AuthEvent(user_id=user_id, event_type="register"))
        session.commit()
        print("Bootstrap account created.")
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit account creation; without this flag, run a dry run.",
    )
    args = parser.parse_args()
    try:
        return run(apply=args.apply)
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
