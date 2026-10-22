"""Read-only local dashboard for reviewing TEST database portfolio imports."""

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.dependencies import get_mf_db
from app.models import (
    DataProvider,
    DataSyncRun,
    FinancialAccount,
    FinancialConnection,
    FinancialInstitution,
    InvestmentHolding,
    MfNav,
    MfScheme,
    SecurityDailyPrice,
    Transaction,
    User,
)
from app.demat_ingestion import _is_valid_isin, value_equity_holding
from app.mf_valuation import value_investment_holding_record

router = APIRouter(prefix="/testing/dashboard", tags=["Testing dashboard"])


def _normalize_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _decimal(value) -> Decimal:
    return Decimal(value) if value is not None else Decimal(0)


def _value_holdings(session: Session, holdings: list[InvestmentHolding], as_of: date) -> list[dict]:
    valued = []
    for holding in holdings:
        quantity = holding.quantity or Decimal(0)
        identifier = holding.asset_identifier
        if holding.asset_type == "MUTUAL_FUND":
            result = value_investment_holding_record(
                session,
                holding,
                as_of_date=as_of,
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
                "name": holding.asset_name,
                "identifier": holding.asset_identifier,
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


def _portfolio_suggestions(holdings: list[dict]) -> list[dict[str, str]]:
    valued = [
        row for row in holdings
        if row["market_value"] is not None and Decimal(row["market_value"]) > 0
    ]
    total_value = sum((Decimal(row["market_value"]) for row in valued), Decimal(0))
    suggestions: list[dict[str, str]] = []
    unpriced_count = sum(row["market_value"] is None for row in holdings)
    stale_count = sum(row["stale"] for row in holdings)

    if unpriced_count:
        suggestions.append(
            {
                "title": "Check price coverage",
                "detail": (
                    f"{unpriced_count} holding(s) have no available local price or NAV. "
                    "Portfolio value and allocation percentages are partial."
                ),
                "kind": "warn",
            }
        )
    if stale_count:
        suggestions.append(
            {
                "title": "Review stale market data",
                "detail": (
                    f"{stale_count} holding(s) are valued using data older than the "
                    "configured freshness window."
                ),
                "kind": "warn",
            }
        )

    if total_value > 0:
        ranked = sorted(
            valued,
            key=lambda row: Decimal(row["market_value"]),
            reverse=True,
        )
        largest = ranked[0]
        largest_share = Decimal(largest["market_value"]) / total_value
        top_three_share = sum(
            (Decimal(row["market_value"]) for row in ranked[:3]),
            Decimal(0),
        ) / total_value
        if largest_share >= Decimal("0.25"):
            suggestions.append(
                {
                    "title": "Review largest holding exposure",
                    "detail": (
                        f"The largest holding is {largest_share:.0%} of the priced "
                        "portfolio. Review whether this exposure is intentional and "
                        "fits your risk tolerance; this is not a trade recommendation."
                    ),
                    "kind": "warn",
                }
            )

        by_type: dict[str, Decimal] = defaultdict(Decimal)
        for row in valued:
            by_type[row["asset_type"]] += Decimal(row["market_value"])
        largest_type, type_value = max(by_type.items(), key=lambda pair: pair[1])
        type_share = type_value / total_value
        if type_share >= Decimal("0.80") and len(by_type) > 1:
            suggestions.append(
                {
                    "title": "Review asset-type mix",
                    "detail": (
                        f"{largest_type} represents {type_share:.0%} of priced holdings. "
                        "Check whether this mix matches your goals and intended "
                        "diversification."
                    ),
                    "kind": "info",
                }
            )

        if len(ranked) >= 3 and top_three_share >= Decimal("0.70"):
            suggestions.append(
                {
                    "title": "Check top-three concentration",
                    "detail": (
                        f"The three largest priced holdings make up "
                        f"{top_three_share:.0%} of the total. Review exposure to "
                        "overlapping risks rather than relying on holding count alone."
                    ),
                    "kind": "warn",
                }
            )

        if len(ranked) >= 20:
            suggestions.append(
                {
                    "title": "Review portfolio complexity",
                    "detail": (
                        f"{len(ranked)} holdings are currently priced. Consider "
                        "reviewing overlap, costs, and whether each position still "
                        "serves a clear purpose."
                    ),
                    "kind": "info",
                }
            )

    if not suggestions:
        suggestions.append(
            {
                "title": "No immediate data-quality flags",
                "detail": (
                    "This is a basic concentration and pricing check, not a full "
                    "suitability or investment recommendation."
                ),
                "kind": "ok",
            }
        )
    return suggestions


def _portfolio_breakdowns(
    holdings: list[dict],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    priced = [
        row
        for row in holdings
        if row["market_value"] is not None
        and Decimal(row["market_value"]) > 0
    ]
    total = sum((Decimal(row["market_value"]) for row in priced), Decimal(0))
    if total <= 0:
        return [], []

    by_type: dict[str, Decimal] = defaultdict(Decimal)
    by_name: dict[str, dict[str, Decimal | str]] = {}
    for row in priced:
        value = Decimal(row["market_value"])
        asset_type = row["asset_type"] or "OTHER"
        by_type[asset_type] += value
        entry = by_name.setdefault(
            row["name"],
            {"name": row["name"], "value": Decimal(0)},
        )
        entry["value"] = Decimal(entry["value"]) + value

    asset_type_rows = [
        {
            "name": asset_type,
            "value": str(value),
            "share": str(value / total * Decimal(100)),
        }
        for asset_type, value in sorted(
            by_type.items(),
            key=lambda pair: pair[1],
            reverse=True,
        )
    ]
    ranked_holdings = sorted(
        by_name.values(),
        key=lambda entry: Decimal(entry["value"]),
        reverse=True,
    )
    top_rows = ranked_holdings[:5]
    if len(ranked_holdings) > 5:
        others_value = sum(
            (Decimal(entry["value"]) for entry in ranked_holdings[5:]),
            Decimal(0),
        )
        top_rows.append({"name": "Other holdings", "value": others_value})
    top_holding_rows = [
        {
            "name": str(entry["name"]),
            "value": str(entry["value"]),
            "share": str(Decimal(entry["value"]) / total * Decimal(100)),
        }
        for entry in top_rows
    ]
    return asset_type_rows, top_holding_rows


def _comparable_snapshot_change(
    previous_values: dict[str, Decimal | None],
    current_values: dict[str, Decimal | None],
) -> dict[str, int | str | None]:
    comparable_ids = {
        identifier
        for identifier in previous_values.keys() & current_values.keys()
        if previous_values[identifier] is not None
        and current_values[identifier] is not None
    }
    previous_total = sum(
        (previous_values[key] for key in comparable_ids),
        Decimal(0),
    )
    current_total = sum(
        (current_values[key] for key in comparable_ids),
        Decimal(0),
    )
    amount = current_total - previous_total
    return {
        "count": len(comparable_ids),
        "previous_value": str(previous_total),
        "current_value": str(current_total),
        "amount": str(amount),
        "percentage": (
            str(amount / previous_total * Decimal(100))
            if previous_total
            else None
        ),
    }


def _dashboard_data(session: Session) -> dict:
    database_name = session.scalar(text("SELECT current_database()"))
    if database_name != "BFAMS-DeFI-test":
        raise HTTPException(
            status_code=503,
            detail="Testing dashboard requires BFAMS-DeFI-test.",
        )

    account = session.scalars(
        select(FinancialAccount).where(
            FinancialAccount.external_account_id == "DHAN-DEMAT-1"
        )
    ).first()
    counts = {
        "users": session.scalar(select(func.count()).select_from(User)),
        "accounts": session.scalar(select(func.count()).select_from(FinancialAccount)),
        "transactions": session.scalar(select(func.count()).select_from(Transaction)),
        "holdings": session.scalar(
            select(func.count()).select_from(InvestmentHolding)
        ),
        "sync_runs": session.scalar(select(func.count()).select_from(DataSyncRun)),
        "providers": session.scalar(select(func.count()).select_from(DataProvider)),
        "institutions": session.scalar(
            select(func.count()).select_from(FinancialInstitution)
        ),
        "mutual_fund_schemes": session.scalar(
            select(func.count()).select_from(MfScheme)
        ),
        "mutual_fund_nav_rows": session.scalar(
            select(func.count()).select_from(MfNav)
        ),
        "nse_price_rows": session.scalar(
            select(func.count()).select_from(SecurityDailyPrice)
        ),
    }

    if account is None:
        return {
            "database": database_name,
            "counts": counts,
            "dhan": {"account_exists": False},
        }

    latest_observed = session.scalar(
        select(InvestmentHolding.observed_at)
        .where(InvestmentHolding.account_id == account.id)
        .order_by(InvestmentHolding.observed_at.desc())
        .limit(1)
    )
    holdings = (
        session.scalars(
            select(InvestmentHolding)
            .where(
                InvestmentHolding.account_id == account.id,
                InvestmentHolding.observed_at == latest_observed,
            )
            .order_by(InvestmentHolding.asset_name)
        ).all()
        if latest_observed is not None
        else []
    )
    current_valuation_date = date.today()
    valued_holdings = _value_holdings(
        session,
        holdings,
        current_valuation_date,
    )
    valued_rows = [
        row for row in valued_holdings if row["market_value"] is not None
    ]
    priced_total = sum(
        (Decimal(row["market_value"]) for row in valued_rows),
        Decimal(0),
    )
    unpriced_count = len(valued_holdings) - len(valued_rows)
    asset_type_allocation, top_holdings = _portfolio_breakdowns(valued_holdings)

    snapshot_dates = session.scalars(
        select(InvestmentHolding.observed_at)
        .where(InvestmentHolding.account_id == account.id)
        .group_by(InvestmentHolding.observed_at)
        .order_by(InvestmentHolding.observed_at.desc())
        .limit(20)
    ).all()
    snapshot_history = []
    snapshot_values_by_identifier: list[dict[str, Decimal | None]] = []
    for observed_at in reversed(snapshot_dates):
        snapshot_rows = session.scalars(
            select(InvestmentHolding).where(
                InvestmentHolding.account_id == account.id,
                InvestmentHolding.observed_at == observed_at,
            )
        ).all()
        history_valued = _value_holdings(
            session,
            snapshot_rows,
            observed_at.date(),
        )
        known_total = sum(
            (
                Decimal(row["market_value"])
                for row in history_valued
                if row["market_value"] is not None
            ),
            Decimal(0),
        )
        snapshot_values_by_identifier.append(
            {
                row["identifier"]: (
                    Decimal(row["market_value"])
                    if row["market_value"] is not None
                    else None
                )
                for row in history_valued
                if row["identifier"]
            }
        )
        snapshot_history.append(
            {
                "observed_at": observed_at.isoformat(),
                "market_value": str(known_total),
                "valued_count": sum(
                    row["market_value"] is not None for row in history_valued
                ),
            }
        )
    comparable_change = {
        "count": 0,
        "previous_value": None,
        "current_value": None,
        "amount": None,
        "percentage": None,
    }
    if len(snapshot_values_by_identifier) >= 2:
        comparable_change = _comparable_snapshot_change(
            snapshot_values_by_identifier[-2],
            snapshot_values_by_identifier[-1],
        )
    transactions = session.scalars(
        select(Transaction).where(Transaction.account_id == account.id)
    ).all()

    holding_groups: dict[str, dict] = {}
    for holding in holdings:
        name_key = _normalize_name(holding.asset_name)
        group = holding_groups.setdefault(
            name_key,
            {
                "name": holding.asset_name,
                "quantity": Decimal(0),
                "market_value": Decimal(0),
                "rows": 0,
            },
        )
        group["quantity"] += _decimal(holding.quantity)
        group["market_value"] += _decimal(holding.market_value)
        group["rows"] += 1

    trade_groups: dict[str, dict] = defaultdict(
        lambda: {
            "net_quantity": Decimal(0),
            "buy_count": 0,
            "sell_count": 0,
            "buy_amount": Decimal(0),
            "sell_amount": Decimal(0),
        }
    )
    buys = sells = unresolved = 0
    for transaction in transactions:
        parts = (transaction.external_transaction_id or "").split("|")
        if len(parts) != 5 or not parts[2].startswith("NAME:"):
            unresolved += 1
            continue
        name_key = parts[2][5:]
        try:
            quantity = Decimal(parts[3])
        except InvalidOperation:
            unresolved += 1
            continue
        group = trade_groups[name_key]
        if parts[1] == "BUY":
            buys += 1
            group["buy_count"] += 1
            group["buy_amount"] += _decimal(transaction.amount)
            group["net_quantity"] += quantity
        elif parts[1] == "SELL":
            sells += 1
            group["sell_count"] += 1
            group["sell_amount"] += _decimal(transaction.amount)
            group["net_quantity"] -= quantity
        else:
            unresolved += 1

    reconciliation = []
    all_names = sorted(set(holding_groups) | set(trade_groups))
    for name_key in all_names:
        holding = holding_groups.get(name_key)
        trade = trade_groups.get(name_key)
        holding_quantity = holding["quantity"] if holding else None
        trade_quantity = trade["net_quantity"] if trade else None
        if holding is None:
            status = "trade_only"
            difference = None
        elif trade is None:
            status = "snapshot_only"
            difference = None
        elif holding_quantity == trade_quantity:
            status = "net_flow_equals_snapshot"
            difference = Decimal(0)
        else:
            status = "opening_position_or_missing_activity"
            difference = holding_quantity - trade_quantity
        reconciliation.append(
            {
                "name": holding["name"] if holding else "Trade group not in snapshot",
                "trade_rows": (
                    trade["buy_count"] + trade["sell_count"] if trade else 0
                ),
                "trade_net_quantity": (
                    str(trade_quantity) if trade_quantity is not None else None
                ),
                "snapshot_quantity": (
                    str(holding_quantity) if holding_quantity is not None else None
                ),
                "quantity_difference": (
                    str(difference) if difference is not None else None
                ),
                "matched": holding is not None and trade is not None,
                "quantity_matches": (
                    holding is not None
                    and trade is not None
                    and holding_quantity == trade_quantity
                ),
                "comparison_status": status,
                "market_value": str(holding["market_value"]) if holding else None,
            }
        )

    matched = [row for row in reconciliation if row["matched"]]
    quantity_matches = sum(1 for row in matched if row["quantity_matches"])
    snapshot_only = sum(1 for row in reconciliation if row["comparison_status"] == "snapshot_only")
    trade_only = sum(1 for row in reconciliation if row["comparison_status"] == "trade_only")
    quantity_differences = len(matched) - quantity_matches
    total_market_value = sum(
        (group["market_value"] for group in holding_groups.values()), Decimal(0)
    )
    total_buy_amount = sum(
        (group["buy_amount"] for group in trade_groups.values()), Decimal(0)
    )
    total_sell_amount = sum(
        (group["sell_amount"] for group in trade_groups.values()), Decimal(0)
    )
    connection = session.get(FinancialConnection, account.connection_id)
    latest_sync = None
    if connection is not None:
        latest_sync = session.scalars(
            select(DataSyncRun)
            .where(DataSyncRun.connection_id == connection.id)
            .order_by(DataSyncRun.created_at.desc())
            .limit(1)
        ).first()

    return {
        "database": database_name,
        "counts": counts,
        "dhan": {
            "account_exists": True,
            "account_type": account.account_type,
            "currency": account.currency,
            "holdings_rows": len(holdings),
            "holdings_market_value": str(priced_total),
            "snapshot_market_value": str(total_market_value),
            "valuation_as_of": current_valuation_date.isoformat(),
            "valued_count": len(valued_rows),
            "unpriced_count": unpriced_count,
            "stale_count": sum(row["stale"] for row in valued_holdings),
            "valuation_coverage": (
                str(Decimal(len(valued_rows)) / Decimal(len(holdings)))
                if holdings
                else "0"
            ),
            "valued_holdings": [
                {key: value for key, value in row.items() if key != "identifier"}
                for row in valued_holdings
            ],
            "asset_type_allocation": asset_type_allocation,
            "top_holdings": top_holdings,
            "snapshot_history": snapshot_history,
            "comparable_change": comparable_change,
            "suggestions": _portfolio_suggestions(valued_holdings),
            "observed_at": latest_observed.isoformat() if latest_observed else None,
            "trade_rows": len(transactions),
            "buy_rows": buys,
            "sell_rows": sells,
            "buy_amount": str(total_buy_amount),
            "sell_amount": str(total_sell_amount),
            "unresolved_trade_rows": unresolved,
            "matched_security_groups": len(matched),
            "quantity_matching_groups": quantity_matches,
            "snapshot_only_groups": snapshot_only,
            "trade_only_groups": trade_only,
            "quantity_difference_groups": quantity_differences,
            "reconciliation": reconciliation,
            "latest_sync": (
                {
                    "type": latest_sync.sync_type,
                    "status": latest_sync.status,
                    "records_processed": latest_sync.records_processed,
                }
                if latest_sync
                else None
            ),
        },
    }


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return HTMLResponse(_DASHBOARD_HTML)


@router.get("/data")
def dashboard_data(session: Session = Depends(get_mf_db)):
    return _dashboard_data(session)


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>BFAMS · Test dashboard</title>
  <style>
    :root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e8edf8;background:#0b1020;font-synthesis:none;text-rendering:optimizeLegibility}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 78% -20%,#1b3150 0,transparent 38%),#0b1020;color:#e8edf8}
    .shell{max-width:1440px;margin:auto;padding:28px 36px 56px}.top{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #253047;padding-bottom:20px}
    .brand{display:flex;gap:12px;align-items:center;font-weight:750;letter-spacing:.02em}.mark{width:36px;height:36px;border-radius:12px;background:linear-gradient(145deg,#78e3c0,#48a7e9);display:grid;place-items:center;color:#08121b;font-weight:900}
    .top nav{display:flex;align-items:center;gap:10px}.top nav a{font-size:12px;color:#c6d2e5;text-decoration:none;border:1px solid #34425d;background:#151d30;border-radius:9px;padding:8px 11px}.top nav a:hover{border-color:#69d5bb}
    .tag{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#92a2bd}.env{border:1px solid #286b60;background:#102a2b;color:#86e3c4;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:700}
    .intro{display:flex;justify-content:space-between;align-items:flex-end;margin:34px 0 22px;gap:16px}.eyebrow{color:#68d9bd;font-size:12px;font-weight:750;letter-spacing:.13em;text-transform:uppercase}
    h1{font-size:clamp(27px,4vw,38px);letter-spacing:-.045em;margin:8px 0}.sub{color:#99a6bd;margin:0;font-size:14px}.button{border:1px solid #34425d;color:#dbe6f8;background:#151d30;border-radius:10px;padding:10px 15px;font-weight:650;cursor:pointer}
    .button:hover{border-color:#69d5bb}.grid{display:grid;gap:14px}.kpis{grid-template-columns:repeat(4,minmax(0,1fr))}.card,.panel{background:linear-gradient(145deg,#151d30,#111827);border:1px solid #263149;border-radius:16px}
    .card{padding:19px}.label{font-size:12px;color:#93a0b7}.value{font-size:27px;font-weight:760;margin:12px 0 4px;letter-spacing:-.04em}.hint{font-size:11px;color:#74829b}
    .section{margin-top:19px}.section-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:11px}.section h2{font-size:15px;margin:0}.panel{padding:19px}.twocol{grid-template-columns:1.15fr .85fr}.statusline{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid #263149}.statusline:last-child{border:0;padding-bottom:0}
    .status-label{font-size:13px;color:#aab6c9}.pill{border-radius:99px;padding:5px 9px;font-size:11px;font-weight:700;background:#172238;color:#a9bad5}.ok{background:#13302c;color:#7be0bd}.warn{background:#392a18;color:#f4c273}.bad{background:#3b2028;color:#ff9eaa}
    .market-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.market{padding:16px}.market strong{display:block;font-size:22px;margin-bottom:5px}.market span{font-size:12px;color:#9aa8be}
    .visual-grid{grid-template-columns:1fr 1fr}.chart{width:100%;height:auto;min-height:250px}.chart text{fill:#aab6c9;font-size:11px;font-family:inherit}.legend{display:grid;gap:8px;margin-top:12px}.legend-row{display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:12px;color:#b8c4d7}.legend-label{display:flex;align-items:center;gap:8px;min-width:0}.legend-dot{width:9px;height:9px;border-radius:50%;flex:none}.legend-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.recommendations{display:grid;gap:10px}.recommendation{padding:13px 15px;border:1px solid #34425d;border-radius:11px;background:#121a2b}.recommendation.warn{border-color:#694c2c}.recommendation.ok{border-color:#286b60}.recommendation strong{display:block;font-size:13px;margin-bottom:5px}.recommendation p{font-size:12px;color:#aab6c9;margin:0;line-height:1.5}.table-wrap.holdings table{min-width:760px}.subtle{font-size:11px;color:#7f8da6;margin:6px 0 0}
    .table-wrap{overflow:auto;border:1px solid #263149;border-radius:12px}table{border-collapse:collapse;width:100%;min-width:700px;text-align:left}th,td{padding:12px 14px;border-bottom:1px solid #253047;font-size:12px;white-space:nowrap}th{color:#8d9bb3;font-weight:650;background:#131b2b}td{color:#dce4f1}tr:last-child td{border-bottom:0}.num{text-align:right;font-variant-numeric:tabular-nums}.positive{color:#81dfbb}.negative{color:#ff9eaa}
    .foot{margin-top:20px;color:#74829a;font-size:11px}.error{color:#ff9eaa}.loading{color:#94a1b8}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px}
    @media(max-width:900px){.shell{padding:20px}.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.twocol{grid-template-columns:1fr}}
    @media(max-width:520px){.shell{padding:16px}.intro{align-items:flex-start;flex-direction:column}.kpis{gap:9px}.card{padding:14px}.value{font-size:23px}.market-grid{grid-template-columns:1fr}}
  </style>
</head>
<body>
<main class="shell">
  <header class="top"><div class="brand"><div class="mark">B</div><div>BFAMS <span class="tag">portfolio lab</span></div></div><nav><a href="/testing/connect">Connect institution</a><span class="env">TEST DATABASE</span></nav></header>
  <section class="intro"><div><div class="eyebrow">Local review workspace</div><h1>Portfolio data health</h1><p class="sub">Review the Dhan import, reconciliation and market-data coverage.</p></div><div><button class="button" id="sync-dhan" type="button">Sync Dhan holdings</button> <button class="button" id="refresh" type="button">↻ &nbsp;Refresh data</button><div id="sync-status" class="foot" aria-live="polite"></div></div></section>
  <section class="grid kpis" id="kpis"><div class="card"><div class="label">Latest snapshot holdings</div><div class="value loading">—</div></div><div class="card"><div class="label">Dhan trades</div><div class="value loading">—</div></div><div class="card"><div class="label">Snapshot value</div><div class="value loading">—</div></div><div class="card"><div class="label">Net flows equal snapshot</div><div class="value loading">—</div></div></section>
  <section class="section panel"><div class="section-head"><h2>Live Dhan positions</h2><button class="button" id="live-valuation" type="button">Fetch from Dhan</button></div><div class="grid market-grid"><div class="market card"><strong id="dhan-holdings-count">—</strong><span>Holdings</span></div><div class="market card"><strong id="dhan-position-count">—</strong><span>Positions</span></div><div class="market card"><strong id="dhan-orders-count">—</strong><span>Today's orders</span></div><div class="market card"><strong id="dhan-realized-pnl">—</strong><span>Realized P&amp;L</span></div><div class="market card"><strong id="dhan-unrealized-pnl">—</strong><span>Unrealized P&amp;L</span></div><div class="market card"><strong id="dhan-day-pnl">—</strong><span>Day P&amp;L</span></div></div><p id="live-valuation-status" class="foot" aria-live="polite">Fetched on request from Dhan Holdings, Positions, and Orders APIs. P&amp;L is not calculated from market quotes.</p><div class="table-wrap"><table><thead><tr><th>Instrument</th><th>Position</th><th>Product</th><th class="num">Net quantity</th><th class="num">Realized P&amp;L</th><th class="num">Unrealized P&amp;L</th><th class="num">Day P&amp;L</th></tr></thead><tbody id="dhan-open-positions"></tbody></table></div></section>
  <section class="section grid visual-grid">
    <div class="panel"><div class="section-head"><h2>Value by holding</h2><span class="pill" id="valuation-coverage">Loading</span></div><p class="subtle" id="valuation-note"></p><svg id="allocation-chart" class="chart" viewBox="0 0 360 260" role="img" aria-label="Current portfolio allocation chart"></svg><div id="allocation-legend" class="legend"></div></div>
    <div class="panel"><div class="section-head"><h2>Portfolio value over snapshots</h2><span class="pill" id="portfolio-change">Loading</span></div><p class="subtle">Valued using locally stored market prices/NAVs as of each snapshot date. Not intraday quotes or returns.</p><svg id="history-chart" class="chart" viewBox="0 0 720 260" role="img" aria-label="Portfolio valuation trend chart"></svg><p class="subtle" id="history-note"></p></div>
    <div class="panel"><div class="section-head"><h2>Asset-type allocation</h2><span class="pill">Priced holdings</span></div><p class="subtle">Shows the mix by asset type. Unpriced positions are excluded.</p><svg id="asset-type-chart" class="chart" viewBox="0 0 440 260" role="img" aria-label="Portfolio allocation by asset type"></svg></div>
    <div class="panel"><div class="section-head"><h2>Largest holdings</h2><span class="pill">Priced holdings</span></div><p class="subtle">Top five positions plus any remaining holdings grouped together.</p><svg id="top-holdings-chart" class="chart" viewBox="0 0 440 260" role="img" aria-label="Largest portfolio holdings by estimated market value"></svg></div>
  </section>
  <section class="section panel"><div class="section-head"><h2>Portfolio review prompts</h2><span class="pill">Educational · informational</span></div><div id="suggestions" class="recommendations"></div><p class="foot">Rule-based prompts based on available data, not personalized financial advice or buy/sell recommendations. Validate goals, time horizon, costs, taxes, and risk tolerance independently.</p></section>
  <section class="section panel"><div class="section-head"><h2>Latest holdings valuation</h2><span class="pill" id="valuation-as-of">Loading</span></div><div class="table-wrap holdings"><table><thead><tr><th>Holding</th><th>Type</th><th class="num">Quantity</th><th class="num">Price / NAV</th><th class="num">Market value</th><th>Price date</th><th>Status</th></tr></thead><tbody id="holdings-valuation"></tbody></table></div></section>
  <section class="section grid twocol">
    <div class="panel"><div class="section-head"><h2>Import health</h2><span class="pill" id="db-name">Loading</span></div><div id="health"></div></div>
    <div class="panel"><div class="section-head"><h2>Reference data coverage</h2><span class="pill ok">Read-only</span></div><div class="grid market-grid" id="markets"></div><p class="foot">Counts are read live from the TEST database.</p></div>
  </section>
  <section class="section panel"><div class="section-head"><h2>Trade-to-snapshot comparison</h2><span class="pill" id="recon-status">Loading</span></div><div class="alert" style="border:1px solid #694c2c;background:#2b2218;color:#f2cb8c;border-radius:11px;padding:13px 14px;font-size:12px;line-height:1.6;margin:0 0 15px">This is not a full position reconciliation: trade net quantity equals current holdings only if the imported trade history starts from zero and includes every trade and position-changing event. Opening positions, transfers, corporate actions, or missing history can explain differences. A missing side is shown as “—”, not as a zero quantity.</div><div class="table-wrap"><table><thead><tr><th>Security group</th><th class="num">Trade rows</th><th class="num">Trade net qty</th><th class="num">Snapshot qty</th><th class="num">Snapshot − net flow</th><th>Status</th></tr></thead><tbody id="reconciliation"></tbody></table></div><p class="foot">Only the latest holdings snapshot is compared. “Snapshot − net flow” is an unexplained starting position/activity amount, not a proven error. Matching names and quantities do not establish complete reconciliation.</p></section>
  <div class="foot" id="updated">Loading data…</div>
</main>
<script>
const byId = id => document.getElementById(id);
const number = value => new Intl.NumberFormat('en-IN').format(Number(value || 0));
const money = value => new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:2}).format(Number(value || 0));
const qty = value => new Intl.NumberFormat('en-IN',{maximumFractionDigits:4}).format(Number(value || 0));
const COLORS=['#68d9bd','#68a8f0','#d6a8ff','#f3c66b','#f28fa0','#7ed1e6','#ffa66b','#a2d889','#b4a7ff','#ed91d0'];
function statusLine(label,value,kind=''){const row=document.createElement('div');row.className='statusline';const l=document.createElement('span');l.className='status-label';l.textContent=label;const v=document.createElement('span');v.className='pill '+kind;v.textContent=value;row.append(l,v);return row}
function svgElement(name,attrs={}){const node=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [key,value] of Object.entries(attrs))node.setAttribute(key,String(value));return node}
function renderAllocation(rows,total){const chart=byId('allocation-chart'),legend=byId('allocation-legend');chart.replaceChildren();legend.replaceChildren();const priced=rows.filter(row=>row.market_value!==null&&Number(row.market_value)>0);if(!priced.length){const text=svgElement('text',{x:180,y:130,'text-anchor':'middle'});text.textContent='No local prices available';chart.append(text);return}const groupTotals=new Map();priced.forEach(row=>groupTotals.set(row.name,(groupTotals.get(row.name)||0)+Number(row.market_value)));const entries=[...groupTotals.entries()].sort((a,b)=>b[1]-a[1]);const circumference=2*Math.PI*76;let offset=0;entries.forEach(([name,value],index)=>{const share=value/total;const color=COLORS[index%COLORS.length];const circle=svgElement('circle',{cx:180,cy:120,r:76,fill:'none',stroke:color,'stroke-width':29,'stroke-dasharray':`${share*circumference} ${circumference-share*circumference}`,'stroke-dashoffset':-offset,transform:'rotate(-90 180 120)'});const title=svgElement('title');title.textContent=`${name}: ${money(value)} (${(share*100).toFixed(1)}%)`;circle.append(title);chart.append(circle);offset+=share*circumference;const line=document.createElement('div');line.className='legend-row';const label=document.createElement('span');label.className='legend-label';const dot=document.createElement('span');dot.className='legend-dot';dot.style.backgroundColor=color;const titleText=document.createElement('span');titleText.className='legend-name';titleText.textContent=name;label.append(dot,titleText);const amount=document.createElement('span');amount.textContent=`${money(value)} · ${(share*100).toFixed(1)}%`;line.append(label,amount);legend.append(line)});const center=svgElement('text',{x:180,y:116,'text-anchor':'middle',style:'font-size:13px'});center.textContent='Priced value';const value=svgElement('text',{x:180,y:139,'text-anchor':'middle',style:'font-size:15px;font-weight:700;fill:#e8edf8'});value.textContent=money(total);chart.append(center,value)}
function renderBreakdownChart(id,rows){const chart=byId(id);chart.replaceChildren();if(!rows.length){const empty=svgElement('text',{x:220,y:130,'text-anchor':'middle'});empty.textContent='No priced holdings available';chart.append(empty);return}const maximum=Math.max(...rows.map(row=>Number(row.value)));const rowHeight=34,top=16,labelWidth=142,barWidth=190;rows.forEach((row,index)=>{const y=top+index*rowHeight,label=svgElement('text',{x:2,y:y+14,'font-size':10});const labelValue=row.name.length>19?row.name.slice(0,18)+'…':row.name;label.textContent=labelValue;const labelTitle=svgElement('title');labelTitle.textContent=row.name;label.append(labelTitle);chart.append(label);const background=svgElement('rect',{x:labelWidth,y:y,width:barWidth,height:17,rx:5,fill:'#202b40'});chart.append(background);const width=Math.max(2,barWidth*Number(row.value)/maximum);const bar=svgElement('rect',{x:labelWidth,y:y,width,height:17,rx:5,fill:COLORS[index%COLORS.length]});const title=svgElement('title');title.textContent=`${row.name}: ${money(row.value)} · ${Number(row.share).toFixed(1)}%`;bar.append(title);chart.append(bar);const share=svgElement('text',{x:labelWidth+barWidth+8,y:y+13,'font-size':10});share.textContent=`${Number(row.share).toFixed(1)}%`;chart.append(share)})}
function renderHistory(history,comparison){const chart=byId('history-chart');chart.replaceChildren();if(!history.length){byId('history-note').textContent='No holdings snapshots yet.';byId('portfolio-change').textContent='No snapshots';return}const width=720,height=260,left=58,right=18,top=20,bottom=45,values=history.map(row=>Number(row.market_value)),min=Math.min(...values),max=Math.max(...values),range=max-min||Math.max(max*.05,1),points=history.map((row,index)=>({x:history.length===1?(left+width-right)/2:left+index*(width-left-right)/(history.length-1),y:top+(max-values[index])*(height-top-bottom)/range,row}));chart.append(svgElement('line',{x1:left,y1:height-bottom,x2:width-right,y2:height-bottom,stroke:'#34425d'}));const path=svgElement('polyline',{points:points.map(point=>`${point.x},${point.y}`).join(' '),fill:'none',stroke:'#68d9bd','stroke-width':3,'stroke-linejoin':'round','stroke-linecap':'round'});chart.append(path);points.forEach(point=>{const circle=svgElement('circle',{cx:point.x,cy:point.y,r:5,fill:'#68d9bd'});const title=svgElement('title');title.textContent=`${new Date(point.row.observed_at).toLocaleString('en-IN')}: ${money(point.row.market_value)} · ${number(point.row.valued_count)} priced`;circle.append(title);chart.append(circle)});const first=history[0],last=history[history.length-1];if(history.length>1){if(comparison&&comparison.amount!==null&&comparison.count>0){const delta=Number(comparison.amount),pct=comparison.percentage===null?null:Number(comparison.percentage);byId('portfolio-change').textContent=`Comparable change: ${delta>=0?'+':''}${money(delta)}${pct===null?'':` (${pct>=0?'+':''}${pct.toFixed(2)}%)`} · ${number(comparison.count)} holdings`;byId('portfolio-change').className='pill '+(delta>=0?'ok':'warn')}else{byId('portfolio-change').textContent='No comparable priced holdings';byId('portfolio-change').className='pill warn'}byId('history-note').textContent=`${new Date(first.observed_at).toLocaleDateString('en-IN')} → ${new Date(last.observed_at).toLocaleDateString('en-IN')} · ${history.length} snapshots. Trend totals may have different price coverage; comparable change uses only positions priced in both latest snapshots and is not investment return.`}else{byId('portfolio-change').textContent='1 snapshot';byId('portfolio-change').className='pill warn';byId('history-note').textContent='Sync again after the cooldown to build a valuation trend.'}const leftLabel=svgElement('text',{x:left,y:height-12});leftLabel.textContent=new Date(first.observed_at).toLocaleDateString('en-IN');chart.append(leftLabel);const rightLabel=svgElement('text',{x:width-right,y:height-12,'text-anchor':'end'});rightLabel.textContent=new Date(last.observed_at).toLocaleDateString('en-IN');chart.append(rightLabel)}
function renderSuggestions(items){const container=byId('suggestions');container.replaceChildren();items.forEach(item=>{const card=document.createElement('article');card.className='recommendation '+(item.kind||'info');const title=document.createElement('strong');title.textContent=item.title;const detail=document.createElement('p');detail.textContent=item.detail;card.append(title,detail);container.append(card)})}
function renderHoldingTable(rows){const body=byId('holdings-valuation');body.replaceChildren();rows.forEach(row=>{const tr=document.createElement('tr');[row.name,row.asset_type,qty(row.quantity),row.price===null?'—':money(row.price),row.market_value===null?'Not priced':money(row.market_value),row.price_date||'—',row.status+(row.stale?' · stale':'')].forEach((value,index)=>{const td=document.createElement('td');td.textContent=value;if(index>=2&&index<=4)td.className='num';tr.append(td)});body.append(tr)})}
async function fetchLiveValuation(){const button=byId('live-valuation'),message=byId('live-valuation-status');button.disabled=true;message.textContent='Requesting holdings, positions and orders from Dhan…';message.className='foot loading';try{const response=await fetch('/portfolio/dhan/live-valuation',{method:'POST',headers:{'Accept':'application/json','X-BFAMS-Sync':'1'},cache:'no-store'});const result=await response.json();if(!response.ok)throw new Error(result.detail||'Live Dhan portfolio request failed.');byId('dhan-holdings-count').textContent=number(result.holdings_count);byId('dhan-position-count').textContent=number(result.position_count);byId('dhan-orders-count').textContent=number(result.orders_count);byId('dhan-realized-pnl').textContent=result.realized_pnl===null?'—':money(result.realized_pnl);byId('dhan-unrealized-pnl').textContent=result.unrealized_pnl===null?'—':money(result.unrealized_pnl);byId('dhan-day-pnl').textContent=result.day_pnl===null?'Not provided':money(result.day_pnl);message.textContent=`Retrieved ${new Date(result.retrieved_at).toLocaleTimeString('en-IN')} · P&L fields are taken from Dhan positions; “Not provided” means Dhan did not return that field.`;message.className='foot';const body=byId('dhan-open-positions');body.replaceChildren();(result.open_positions||[]).forEach(position=>{const row=document.createElement('tr');[position.trading_symbol||'—',position.position_type||'—',position.product_type||'—',position.net_quantity===null?'—':qty(position.net_quantity),position.realized_pnl===null?'—':money(position.realized_pnl),position.unrealized_pnl===null?'—':money(position.unrealized_pnl),position.day_pnl===null?'—':money(position.day_pnl)].forEach((value,index)=>{const cell=document.createElement('td');cell.textContent=value;if(index>=3)cell.className='num';row.append(cell)});body.append(row)});if(!result.open_positions?.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=7;cell.textContent='No open positions returned by Dhan.';row.append(cell);body.append(row)}}catch(error){message.textContent=error.message;message.className='foot error'}finally{button.disabled=false}}
async function load(){byId('updated').textContent='Refreshing…';byId('updated').className='foot loading';try{const response=await fetch('/testing/dashboard/data',{cache:'no-store'});if(!response.ok)throw new Error('Dashboard data request failed ('+response.status+')');const data=await response.json();const d=data.dhan||{};const counts=data.counts||{};
  byId('db-name').textContent=data.database||'Unknown database';byId('sync-dhan').dataset.accountAvailable=String(Boolean(d.account_exists));byId('sync-dhan').disabled=!d.account_exists;
  document.querySelector('#kpis .card:nth-child(3) .label').textContent='Priced market value';
  const kpiValues=[number(d.holdings_rows),number(d.trade_rows),money(d.holdings_market_value),`${number(d.quantity_matching_groups||0)} / ${number(d.matched_security_groups||0)}`];
  [...byId('kpis').children].forEach((card,i)=>{card.querySelector('.value').textContent=kpiValues[i]});
  const coverage=Number(d.valuation_coverage||0);byId('valuation-coverage').textContent=`${number(d.valued_count)} / ${number(d.holdings_rows)} priced`;byId('valuation-note').textContent=`${number(d.unpriced_count)} holding(s) are excluded from the current estimated total. Price date: ${d.valuation_as_of||'—'}.`;byId('valuation-as-of').textContent=`As of ${d.valuation_as_of||'—'} · ${Math.round(coverage*100)}% coverage`;renderAllocation(d.valued_holdings||[],Number(d.holdings_market_value||0));renderHistory(d.snapshot_history||[],d.comparable_change);renderBreakdownChart('asset-type-chart',d.asset_type_allocation||[]);renderBreakdownChart('top-holdings-chart',d.top_holdings||[]);renderSuggestions(d.suggestions||[]);renderHoldingTable(d.valued_holdings||[]);
  const health=byId('health');health.replaceChildren();
  health.append(statusLine('Dhan demat account',d.account_exists?'Connected':'Not found',d.account_exists?'ok':'bad'));
  health.append(statusLine('Holdings snapshot',`${number(d.holdings_rows)} rows · ${d.observed_at?new Date(d.observed_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}):'No timestamp'}`,d.holdings_rows?'ok':'warn'));
  health.append(statusLine('Trades',`${number(d.trade_rows)} total · ${number(d.buy_rows)} buy · ${number(d.sell_rows)} sell`,d.unresolved_trade_rows?'warn':'ok'));
  health.append(statusLine('Latest sync',d.latest_sync?`${d.latest_sync.status} · ${number(d.latest_sync.records_processed)} rows`:'No sync record',d.latest_sync?.status==='completed'?'ok':'warn'));
  const markets=byId('markets');markets.replaceChildren();
  [['NSE prices',counts.nse_price_rows],['MF NAV observations',counts.mutual_fund_nav_rows],['MF schemes',counts.mutual_fund_schemes]].forEach(([label,value])=>{const box=document.createElement('div');box.className='market card';const strong=document.createElement('strong');strong.textContent=number(value);const span=document.createElement('span');span.textContent=label;box.append(strong,span);markets.append(box)});
  const body=byId('reconciliation');body.replaceChildren();
  (d.reconciliation||[]).forEach(row=>{const tr=document.createElement('tr');const display=value=>value===null||value===undefined?'—':qty(value);const values=[row.name,number(row.trade_rows),display(row.trade_net_quantity),display(row.snapshot_quantity),display(row.quantity_difference)];values.forEach((value,index)=>{const td=document.createElement('td');td.textContent=value;if(index>0)td.className='num';if(index===4&&row.quantity_difference!==null)td.classList.add(Number(row.quantity_difference)===0?'positive':'negative');tr.append(td)});const td=document.createElement('td');const badge=document.createElement('span');const status=row.comparison_status;badge.className='pill '+(status==='net_flow_equals_snapshot'?'ok':status==='opening_position_or_missing_activity'?'bad':'warn');badge.textContent=status==='net_flow_equals_snapshot'?'Net flow equals snapshot':status==='opening_position_or_missing_activity'?'Opening position / activity needed':status==='snapshot_only'?'No trades in imported file':'Not in snapshot';td.append(badge);tr.append(td);body.append(tr)});
  const matched=(d.matched_security_groups||0),same=(d.quantity_matching_groups||0);byId('recon-status').textContent=`${number(same)} / ${number(matched)} net flows equal snapshot`;byId('recon-status').className='pill '+(same===matched&&matched>0?'ok':'warn');
  byId('updated').textContent='Live TEST data · refreshed '+new Date().toLocaleTimeString('en-IN');byId('updated').className='foot';
 }catch(error){byId('updated').textContent=error.message;byId('updated').className='foot error'}}
async function syncDhan(){const button=byId('sync-dhan'),status=byId('sync-status');button.disabled=true;status.textContent='Fetching dry-run counts…';status.className='foot loading';const headers={'Accept':'application/json','X-BFAMS-Sync':'1'};try{const previewResponse=await fetch('/portfolio/dhan/sync?dry_run=true',{method:'POST',headers});const preview=await previewResponse.json();if(!previewResponse.ok)throw new Error(preview.detail||'Dhan dry run failed.');const countText=`Fetched: ${number(preview.fetched)}\nTo insert: ${number(preview.to_insert)}\nSkipped zero quantity: ${number(preview.skipped_zero_qty)}\nSame quantity: ${number(preview.same_quantity)}\nDifferent quantity: ${number(preview.different_quantity)}\nNew in API: ${number(preview.new_in_api)}\nAbsent from API: ${number(preview.absent_from_api)}\nInferred from symbol only: ${number(preview.inferred_from_symbol_only)}\nWithout ISIN: ${number(preview.without_isin)}`;status.textContent=countText;if(!window.confirm(`Dhan dry-run counts (no records written):\n\n${countText}\n\nProceed with saving the holdings snapshot?`)){status.className='foot';return}status.textContent='Saving holdings snapshot…';const applyResponse=await fetch('/portfolio/dhan/sync?dry_run=false',{method:'POST',headers});const result=await applyResponse.json();if(!applyResponse.ok)throw new Error(result.detail||'Dhan holdings sync failed.');status.textContent=`Completed. ${countText}`;status.className='foot';await load()}catch(error){status.textContent=error.message;status.className='foot error'}finally{button.disabled=button.dataset.accountAvailable!=='true'}}
byId('refresh').addEventListener('click',load);byId('sync-dhan').addEventListener('click',syncDhan);byId('live-valuation').addEventListener('click',fetchLiveValuation);load();window.setInterval(load,60000);
</script>
</body>
</html>"""
