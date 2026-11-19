"""Portfolio valuation and daily history persistence."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.demat_ingestion import _is_valid_isin, value_equity_holding
from app.mf_valuation import value_investment_holding_record
from app.models import (
    DataProvider,
    FinancialAccount,
    FinancialConnection,
    InvestmentHolding,
    PortfolioValuationSnapshot,
    PortfolioValuationSnapshotItem,
)


MAX_HISTORY_DAYS = 366
MAX_HOLDINGS_AGE_DAYS = 3
MAX_EQUITY_PRICE_AGE_DAYS = 3
MAX_MF_NAV_AGE_DAYS = 4


def _decimal(value) -> Decimal:
    return Decimal(value) if value is not None else Decimal(0)


def value_holdings(
    session: Session,
    holdings: list[InvestmentHolding],
    as_of: date,
) -> list[dict]:
    valued = []
    for holding in holdings:
        quantity = holding.quantity or Decimal(0)
        identifier = holding.asset_identifier
        if holding.asset_type == "MUTUAL_FUND":
            result = value_investment_holding_record(
                session,
                holding,
                as_of_date=as_of,
                max_staleness_days=MAX_MF_NAV_AGE_DAYS,
            )
            price = result.nav
            price_date = result.nav_date
            market_value = result.market_value
            status = result.status.value
            stale = result.is_stale
        elif identifier and _is_valid_isin(identifier):
            result = value_equity_holding(
                session=session,
                isin=identifier,
                asset_name=holding.asset_name,
                asset_type=holding.asset_type,
                quantity=quantity,
                as_of_date=as_of,
                max_staleness_days=MAX_EQUITY_PRICE_AGE_DAYS,
                dhan_closing_price=holding.unit_price,
            )
            price = result.close_price
            price_date = result.price_date
            market_value = result.market_value
            status = result.status
            stale = result.is_stale
        else:
            price = holding.unit_price
            price_date = holding.observed_at.date()
            market_value = holding.market_value
            status = "snapshot" if market_value is not None else "no_price"
            stale = False

        valued.append(
            {
                "holding_id": holding.id,
                "name": holding.asset_name,
                "identifier": identifier,
                "asset_type": holding.asset_type,
                "quantity": str(quantity),
                "price": str(price) if price is not None else None,
                "price_date": price_date.isoformat() if price_date else None,
                "market_value": (
                    str(market_value) if market_value is not None else None
                ),
                "status": status,
                "stale": stale,
            }
        )
    return valued


def _latest_holdings(
    session: Session,
    account_id: uuid.UUID,
) -> tuple[datetime | None, list[InvestmentHolding]]:
    observed_at = session.scalar(
        select(func.max(InvestmentHolding.observed_at)).where(
            InvestmentHolding.account_id == account_id
        )
    )
    if observed_at is None:
        return None, []
    holdings = session.scalars(
        select(InvestmentHolding)
        .where(
            InvestmentHolding.account_id == account_id,
            InvestmentHolding.observed_at == observed_at,
        )
        .order_by(InvestmentHolding.asset_name, InvestmentHolding.id)
    ).all()
    return observed_at, holdings


def capture_daily_portfolio_snapshots(
    session: Session,
    as_of_date: date,
) -> list[dict]:
    accounts = session.scalars(
        select(FinancialAccount)
        .join(
            FinancialConnection,
            FinancialConnection.id == FinancialAccount.connection_id,
        )
        .join(DataProvider, DataProvider.id == FinancialConnection.provider_id)
        .where(
            DataProvider.provider_code == "dhan_manual",
            FinancialAccount.status == "active",
        )
        .order_by(FinancialAccount.id)
    ).all()
    results = []

    for account in accounts:
        observed_at, holdings = _latest_holdings(session, account.id)
        if observed_at is None or not holdings:
            continue

        valued = value_holdings(session, holdings, as_of_date)
        priced_rows = [
            row for row in valued if row["market_value"] is not None
        ]
        total_market_value = sum(
            (Decimal(row["market_value"]) for row in priced_rows),
            Decimal(0),
        )
        price_dates = [
            date.fromisoformat(row["price_date"])
            for row in valued
            if row["price_date"] is not None
        ]
        snapshot = session.scalar(
            select(PortfolioValuationSnapshot).where(
                PortfolioValuationSnapshot.account_id == account.id,
                PortfolioValuationSnapshot.valuation_date == as_of_date,
            )
        )
        if snapshot is None:
            snapshot = PortfolioValuationSnapshot(
                id=uuid.uuid4(),
                account_id=account.id,
                valuation_date=as_of_date,
                holdings_observed_at=observed_at,
                total_market_value=total_market_value,
                total_holdings=len(valued),
                priced_holdings=len(priced_rows),
                unpriced_holdings=len(valued) - len(priced_rows),
                stale_holdings=sum(row["stale"] for row in valued),
                market_data_as_of=max(price_dates) if price_dates else None,
            )
            session.add(snapshot)
            session.flush()
        else:
            snapshot.holdings_observed_at = observed_at
            snapshot.total_market_value = total_market_value
            snapshot.total_holdings = len(valued)
            snapshot.priced_holdings = len(priced_rows)
            snapshot.unpriced_holdings = len(valued) - len(priced_rows)
            snapshot.stale_holdings = sum(row["stale"] for row in valued)
            snapshot.market_data_as_of = max(price_dates) if price_dates else None
            session.execute(
                delete(PortfolioValuationSnapshotItem).where(
                    PortfolioValuationSnapshotItem.snapshot_id == snapshot.id
                )
            )

        session.add_all(
            [
                PortfolioValuationSnapshotItem(
                    snapshot_id=snapshot.id,
                    holding_id=row["holding_id"],
                    asset_name=row["name"],
                    asset_identifier=row["identifier"],
                    asset_type=row["asset_type"],
                    quantity=Decimal(row["quantity"]),
                    unit_price=(
                        Decimal(row["price"]) if row["price"] is not None else None
                    ),
                    market_value=(
                        Decimal(row["market_value"])
                        if row["market_value"] is not None
                        else None
                    ),
                    currency=account.currency or "INR",
                    price_date=(
                        date.fromisoformat(row["price_date"])
                        if row["price_date"] is not None
                        else None
                    ),
                    status=row["status"],
                    is_stale=row["stale"],
                )
                for row in valued
            ]
        )
        results.append(
            {
                "account_id": account.id,
                "valuation_date": as_of_date,
                "holdings_observed_at": observed_at,
                "total_market_value": total_market_value,
                "total_holdings": len(valued),
                "priced_holdings": len(priced_rows),
                "unpriced_holdings": len(valued) - len(priced_rows),
                "stale_holdings": sum(row["stale"] for row in valued),
            }
        )

    session.flush()
    return results


def load_daily_portfolio_history(
    session: Session,
    account_id: uuid.UUID,
    as_of_date: date,
) -> tuple[list[dict], dict | None]:
    first_date = as_of_date - timedelta(days=MAX_HISTORY_DAYS - 1)
    snapshots = session.scalars(
        select(PortfolioValuationSnapshot)
        .where(
            PortfolioValuationSnapshot.account_id == account_id,
            PortfolioValuationSnapshot.valuation_date >= first_date,
            PortfolioValuationSnapshot.valuation_date <= as_of_date,
        )
        .order_by(PortfolioValuationSnapshot.valuation_date)
    ).all()
    if not snapshots:
        return [], None

    snapshot_ids = [snapshot.id for snapshot in snapshots]
    items = session.scalars(
        select(PortfolioValuationSnapshotItem).where(
            PortfolioValuationSnapshotItem.snapshot_id.in_(snapshot_ids)
        )
    ).all()
    items_by_snapshot: dict[uuid.UUID, list[PortfolioValuationSnapshotItem]] = {}
    for item in items:
        items_by_snapshot.setdefault(item.snapshot_id, []).append(item)

    history = []
    for snapshot in snapshots:
        type_totals: dict[str, Decimal] = {}
        for item in items_by_snapshot.get(snapshot.id, []):
            if item.market_value is not None:
                type_totals[item.asset_type] = (
                    type_totals.get(item.asset_type, Decimal(0))
                    + item.market_value
                )
        allocation = [
            {
                "asset_type": asset_type,
                "market_value": str(value),
                "share": (
                    str(value / snapshot.total_market_value * Decimal(100))
                    if snapshot.total_market_value
                    else "0"
                ),
            }
            for asset_type, value in sorted(
                type_totals.items(),
                key=lambda entry: entry[1],
                reverse=True,
            )
        ]
        history.append(
            {
                "observed_at": snapshot.valuation_date.isoformat(),
                "valuation_date": snapshot.valuation_date.isoformat(),
                "market_value": str(snapshot.total_market_value),
                "valued_count": snapshot.priced_holdings,
                "total_holdings": snapshot.total_holdings,
                "unpriced_count": snapshot.unpriced_holdings,
                "stale_count": snapshot.stale_holdings,
                "holdings_observed_at": snapshot.holdings_observed_at.isoformat(),
                "market_data_as_of": (
                    snapshot.market_data_as_of.isoformat()
                    if snapshot.market_data_as_of
                    else None
                ),
                "allocations": allocation,
            }
        )

    latest = snapshots[-1]
    holdings_age_days = max(
        (as_of_date - latest.holdings_observed_at.date()).days,
        0,
    )
    valuation_age_days = (as_of_date - latest.valuation_date).days
    warnings = []
    if latest.unpriced_holdings:
        warnings.append(
            f"{latest.unpriced_holdings} holding(s) have no price in the latest daily valuation."
        )
    if latest.stale_holdings:
        warnings.append(
            f"{latest.stale_holdings} holding(s) use stale prices or NAVs."
        )
    if holdings_age_days > MAX_HOLDINGS_AGE_DAYS:
        warnings.append(
            f"Holdings were last synced {holdings_age_days} days ago."
        )
    if valuation_age_days > MAX_HOLDINGS_AGE_DAYS:
        warnings.append(
            f"The latest daily valuation is {valuation_age_days} days old."
        )
    if latest.market_data_as_of is None:
        warnings.append("No dated market prices or NAVs were available.")

    freshness = {
        "valuation_date": latest.valuation_date.isoformat(),
        "valuation_age_days": valuation_age_days,
        "holdings_observed_at": latest.holdings_observed_at.isoformat(),
        "holdings_age_days": holdings_age_days,
        "market_data_as_of": (
            latest.market_data_as_of.isoformat()
            if latest.market_data_as_of
            else None
        ),
        "priced_holdings": latest.priced_holdings,
        "total_holdings": latest.total_holdings,
        "unpriced_holdings": latest.unpriced_holdings,
        "stale_holdings": latest.stale_holdings,
        "warnings": warnings,
    }
    return history, freshness
