"""Preview or create one isolated Setu sandbox connection in the TEST database."""

import argparse

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models import DataProvider, FinancialConnection, User
from database import get_test_engine

TEST_DATABASE_NAME = "BFAMS-DeFI-test"
PROVIDER_CODE = "setu_sandbox"
PROVIDER_NAME = "Setu sandbox"
PROVIDER_TYPE = "account_aggregator"
EXTERNAL_CONNECTION_ID = "SETU-SANDBOX-1"


def _require_test_database(session: Session) -> None:
    if session.scalar(text("SELECT current_database()")) != TEST_DATABASE_NAME:
        raise RuntimeError("Setu sandbox setup requires the TEST database.")


def setup_sandbox_connection(session: Session, *, apply: bool) -> dict[str, int]:
    _require_test_database(session)
    provider = session.scalars(
        select(DataProvider).where(DataProvider.provider_code == PROVIDER_CODE)
    ).first()

    counts = {"providers_created": 0, "users_created": 0, "connections_created": 0}
    if provider is None:
        counts["providers_created"] = 1
        provider = DataProvider(
            provider_code=PROVIDER_CODE,
            provider_name=PROVIDER_NAME,
            provider_type=PROVIDER_TYPE,
        )
        if apply:
            _require_test_database(session)
            session.add(provider)
            session.flush()
    elif (
        provider.provider_name != PROVIDER_NAME
        or provider.provider_type != PROVIDER_TYPE
    ):
        raise RuntimeError("Existing Setu sandbox provider configuration conflicts.")

    connection = session.scalars(
        select(FinancialConnection).where(
            FinancialConnection.external_connection_id == EXTERNAL_CONNECTION_ID
        )
    ).first()
    if connection is not None:
        if connection.provider_id != provider.id:
            raise RuntimeError("Setu sandbox connection ID belongs to another provider.")
        connection_count = session.scalar(
            select(func.count())
            .select_from(FinancialConnection)
            .where(FinancialConnection.user_id == connection.user_id)
        )
        if connection_count != 1:
            raise RuntimeError("Setu sandbox user is not isolated.")
        if apply:
            session.commit()
        return counts

    existing_setu_connections = session.scalar(
        select(func.count())
        .select_from(FinancialConnection)
        .where(FinancialConnection.provider_id == provider.id)
    )
    if existing_setu_connections:
        raise RuntimeError("Unexpected Setu sandbox connections already exist.")

    counts["users_created"] = 1
    counts["connections_created"] = 1
    if apply:
        user = User()
        _require_test_database(session)
        session.add(user)
        session.flush()

        connection = FinancialConnection(
            user_id=user.id,
            provider_id=provider.id,
            external_connection_id=EXTERNAL_CONNECTION_ID,
            status="pending",
        )
        _require_test_database(session)
        session.add(connection)
        session.flush()
        session.commit()

    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="insert missing Setu sandbox provider, user and connection",
    )
    args = parser.parse_args()

    with Session(get_test_engine()) as session:
        try:
            counts = setup_sandbox_connection(session, apply=args.apply)
            if args.apply:
                print("mode=apply")
            else:
                session.rollback()
                print("mode=dry_run")
            for name, count in counts.items():
                print(f"{name}={count}")
        except Exception as exc:
            session.rollback()
            print(type(exc).__name__)
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
