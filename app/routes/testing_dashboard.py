"""Read-only local dashboard for reviewing TEST database portfolio imports."""

import re
from collections import defaultdict
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

router = APIRouter(prefix="/testing/dashboard", tags=["Testing dashboard"])


def _normalize_name(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def _decimal(value) -> Decimal:
    return Decimal(value) if value is not None else Decimal(0)


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
            "holdings_market_value": str(total_market_value),
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
function statusLine(label,value,kind=''){const row=document.createElement('div');row.className='statusline';const l=document.createElement('span');l.className='status-label';l.textContent=label;const v=document.createElement('span');v.className='pill '+kind;v.textContent=value;row.append(l,v);return row}
async function load(){byId('updated').textContent='Refreshing…';byId('updated').className='foot loading';try{const response=await fetch('/testing/dashboard/data',{cache:'no-store'});if(!response.ok)throw new Error('Dashboard data request failed ('+response.status+')');const data=await response.json();const d=data.dhan||{};const counts=data.counts||{};
  byId('db-name').textContent=data.database||'Unknown database';byId('sync-dhan').dataset.accountAvailable=String(Boolean(d.account_exists));byId('sync-dhan').disabled=!d.account_exists;
  const kpiValues=[number(d.holdings_rows),number(d.trade_rows),money(d.holdings_market_value),`${number(d.quantity_matching_groups||0)} / ${number(d.matched_security_groups||0)}`];
  [...byId('kpis').children].forEach((card,i)=>{card.querySelector('.value').textContent=kpiValues[i]});
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
byId('refresh').addEventListener('click',load);byId('sync-dhan').addEventListener('click',syncDhan);load();
</script>
</body>
</html>"""
