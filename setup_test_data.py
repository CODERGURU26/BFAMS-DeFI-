from sqlalchemy import func, select
from sqlalchemy.orm import Session

from database import get_test_engine
from app.models import DataProvider, FinancialConnection, User

TEST_CONNECTION_ID = "TEST-CONNECTION-1"

engine = get_test_engine()

with Session(engine) as session:
    provider = session.scalars(
        select(DataProvider).where(DataProvider.provider_code == "finvu")
    ).first()
    if provider is None:
        provider = DataProvider(
            provider_code="finvu",
            provider_name="Finvu Account Aggregator",
            provider_type="account_aggregator",
        )
        session.add(provider)
        session.flush()

    connection = session.scalars(
        select(FinancialConnection).where(
            FinancialConnection.external_connection_id == TEST_CONNECTION_ID
        )
    ).first()
    if connection is None:
        user = User()
        session.add(user)
        session.flush()
        connection = FinancialConnection(
            user_id=user.id,
            provider_id=provider.id,
            external_connection_id=TEST_CONNECTION_ID,
            status="active",
        )
        session.add(connection)

    session.commit()

    print("providers:", session.scalar(select(func.count()).select_from(DataProvider)))
    print("users:", session.scalar(select(func.count()).select_from(User)))
    print("connections:", session.scalar(select(func.count()).select_from(FinancialConnection)))