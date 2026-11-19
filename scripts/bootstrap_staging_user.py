"""Create a fresh staging login and its Dhan demat account."""

import argparse
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth.schemas import is_valid_email
from app.auth.security import PasswordPolicyError, normalize_email, validate_password
from app.models import (
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    FinancialInstitution,
    UserCredential,
)
from database import engine
from scripts.create_auth_user import create_auth_user


DATABASE_NAME = "BFAMS-DeFI"
STAGING_EMAIL_HELP = "Email for the new staging login."


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True, help=STAGING_EMAIL_HELP)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create the staging login, Dhan connection, and account.",
    )
    return parser.parse_args(argv)


def _verify_database(session: Session) -> None:
    database_name = session.execute(
        text("SELECT current_database()")
    ).scalar_one()
    if database_name != DATABASE_NAME:
        raise RuntimeError("Staging bootstrap requires BFAMS-DeFI.")


def _ensure_single_result(session: Session, model, **filters):
    rows = session.scalars(select(model).filter_by(**filters)).all()
    if len(rows) > 1:
        raise RuntimeError(
            f"Multiple {model.__tablename__} rows match bootstrap data."
        )
    return rows[0] if rows else None


def _check_can_create(session: Session, email: str) -> None:
    if session.scalar(
        select(UserCredential.user_id).where(UserCredential.email == email)
    ) is not None:
        raise ValueError("A staging login already exists for this email.")

    existing_account = session.scalar(
        select(FinancialAccount.id)
        .join(
            FinancialConnection,
            FinancialConnection.id == FinancialAccount.connection_id,
        )
        .join(
            DataProvider,
            DataProvider.id == FinancialConnection.provider_id,
        )
        .where(
            DataProvider.provider_code == "dhan_manual",
            FinancialAccount.external_account_id == "DHAN-DEMAT-1",
        )
        .limit(1)
    )
    if existing_account is not None:
        raise RuntimeError(
            "A staging Dhan account already exists; refusing to create another."
        )


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    email = normalize_email(args.email)
    if not is_valid_email(email):
        raise ValueError("Invalid email.")

    with Session(engine) as session:
        _verify_database(session)
        _check_can_create(session, email)

        if not args.apply:
            print(
                "Dry run: would create a fresh staging login, "
                "Dhan connection, and demat account."
            )
            return 0

        session.rollback()
        password = getpass.getpass("Staging password: ")
        confirmation = getpass.getpass("Confirm staging password: ")
        if password != confirmation:
            raise ValueError("Passwords do not match.")
        try:
            validate_password(password, email)
        except PasswordPolicyError:
            raise ValueError("Password does not meet authentication policy.") from None

        session.begin()
        try:
            _verify_database(session)
            _check_can_create(session, email)
            user_id = create_auth_user(
                session,
                email=email,
                password=password,
            )

            provider = _ensure_single_result(
                session,
                DataProvider,
                provider_code="dhan_manual",
            )
            if provider is None:
                provider = DataProvider(
                    provider_code="dhan_manual",
                    provider_name="Dhan",
                    provider_type="manual_import",
                )
                session.add(provider)
                session.flush()

            institution = _ensure_single_result(
                session,
                FinancialInstitution,
                institution_code="DHAN",
            )
            if institution is None:
                institution = FinancialInstitution(
                    institution_name="Dhan",
                    institution_code="DHAN",
                    institution_type="broker",
                    country_code="IN",
                )
                session.add(institution)
                session.flush()

            connection = FinancialConnection(
                user_id=user_id,
                provider_id=provider.id,
                external_connection_id="DHAN-MANUAL-1",
                status="active",
            )
            session.add(connection)
            session.flush()
            session.add(
                FinancialAccount(
                    connection_id=connection.id,
                    institution_id=institution.id,
                    external_account_id="DHAN-DEMAT-1",
                    account_type="demat",
                    account_name="Dhan Demat Account",
                    currency="INR",
                    status="active",
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise

    print("Staging login and Dhan account created.")
    return 0


def main() -> int:
    try:
        return run()
    except (RuntimeError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
