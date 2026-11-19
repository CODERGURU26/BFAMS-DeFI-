from datetime import date, datetime, timezone
from decimal import Decimal
import uuid
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    FinancialInstitution,
    InvestmentHolding,
    PortfolioValuationSnapshot,
    PortfolioValuationSnapshotItem,
    User,
)
from app.portfolio_history import (
    capture_daily_portfolio_snapshots,
    load_daily_portfolio_history,
)


def build_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    tables = [
        User.__table__,
        DataProvider.__table__,
        FinancialInstitution.__table__,
        FinancialConnection.__table__,
        FinancialAccount.__table__,
        InvestmentHolding.__table__,
        PortfolioValuationSnapshot.__table__,
        PortfolioValuationSnapshotItem.__table__,
    ]
    Base.metadata.create_all(engine, tables=tables)
    session = Session(engine)

    user_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    institution_id = uuid.uuid4()
    connection_id = uuid.uuid4()
    account_id = uuid.uuid4()
    session.add_all(
        [
            User(id=user_id),
            DataProvider(
                id=provider_id,
                provider_code="dhan_manual",
                provider_name="Dhan",
                provider_type="manual_import",
            ),
            FinancialInstitution(
                id=institution_id,
                institution_name="Dhan",
                institution_code="DHAN",
                institution_type="broker",
            ),
            FinancialConnection(
                id=connection_id,
                user_id=user_id,
                provider_id=provider_id,
                status="active",
            ),
            FinancialAccount(
                id=account_id,
                connection_id=connection_id,
                institution_id=institution_id,
                external_account_id="DHAN-DEMAT-1",
                account_type="demat",
                currency="INR",
                status="active",
            ),
        ]
    )
    session.commit()
    return session, account_id


def add_holding(session, account_id, *, observed_at, asset_name, market_value):
    holding = InvestmentHolding(
        id=uuid.uuid4(),
        account_id=account_id,
        asset_name=asset_name,
        asset_type="EQUITY",
        quantity=Decimal("2"),
        unit_price=Decimal("50") if market_value is not None else None,
        market_value=market_value,
        currency="INR",
        observed_at=observed_at,
    )
    session.add(holding)
    session.commit()
    return holding


def test_daily_snapshot_is_idempotent_and_replaces_latest_holdings():
    session, account_id = build_session()
    first_sync = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
    add_holding(
        session,
        account_id,
        observed_at=first_sync,
        asset_name="Holding A",
        market_value=Decimal("100"),
    )
    valuation_date = date(2026, 11, 19)

    first = capture_daily_portfolio_snapshots(session, valuation_date)
    session.commit()
    assert first[0]["total_market_value"] == Decimal("100")

    second_sync = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)
    add_holding(
        session,
        account_id,
        observed_at=second_sync,
        asset_name="Holding B",
        market_value=Decimal("250"),
    )
    add_holding(
        session,
        account_id,
        observed_at=second_sync,
        asset_name="Unpriced",
        market_value=None,
    )
    second = capture_daily_portfolio_snapshots(session, valuation_date)
    session.commit()

    assert second[0]["total_market_value"] == Decimal("250")
    assert second[0]["total_holdings"] == 2
    assert second[0]["unpriced_holdings"] == 1
    assert session.scalar(
        select(func.count()).select_from(PortfolioValuationSnapshot)
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(PortfolioValuationSnapshotItem)
    ) == 2

    history, freshness = load_daily_portfolio_history(
        session,
        account_id,
        valuation_date,
    )
    assert len(history) == 1
    assert history[0]["market_value"] == "250.0000"
    assert history[0]["allocations"][0]["asset_type"] == "EQUITY"
    assert freshness["unpriced_holdings"] == 1
    assert freshness["warnings"]
    session.close()


def test_stale_price_is_persisted_and_warned():
    session, account_id = build_session()
    observed_at = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
    holding = add_holding(
        session,
        account_id,
        observed_at=observed_at,
        asset_name="Holding A",
        market_value=Decimal("100"),
    )
    stale_valuation = {
        "holding_id": holding.id,
        "name": holding.asset_name,
        "identifier": holding.asset_identifier,
        "asset_type": holding.asset_type,
        "quantity": "2",
        "price": "50",
        "price_date": "2026-11-10",
        "market_value": "100",
        "status": "stale",
        "stale": True,
    }

    with patch(
        "app.portfolio_history.value_holdings",
        return_value=[stale_valuation],
    ):
        results = capture_daily_portfolio_snapshots(
            session,
            date(2026, 11, 19),
        )
    session.commit()

    history, freshness = load_daily_portfolio_history(
        session,
        account_id,
        date(2026, 11, 19),
    )
    assert results[0]["stale_holdings"] == 1
    assert history[0]["stale_count"] == 1
    assert any("stale prices" in warning for warning in freshness["warnings"])
    session.close()
