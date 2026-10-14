import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import re
from typing import Any, Dict, List, Optional, Tuple, Union
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FinancialAccount, InvestmentHolding, Transaction


# Default column aliases matching Zerodha, Groww, Upstox, ICICI Direct, HDFC Sky, CAS, etc.
DEFAULT_COLUMN_ALIASES: Dict[str, List[str]] = {
    "symbol": [
        "symbol",
        "stock_symbol",
        "ticker",
        "scrip_symbol",
        "trading_symbol",
        "instrument",
    ],
    "isin": ["isin", "isin_code", "instrument_isin", "security_isin"],
    "asset_name": [
        "instrument_name",
        "asset_name",
        "company_name",
        "scrip_name",
        "security_name",
        "particulars",
        "name",
    ],
    "quantity": [
        "quantity",
        "qty",
        "units",
        "shares",
        "current_quantity",
        "holding_qty",
        "net_qty",
    ],
    "price": [
        "price",
        "avg_cost",
        "buy_avg",
        "average_price",
        "cost_price",
        "avg_price",
        "rate",
        "trade_price",
    ],
    "market_value": [
        "market_value",
        "current_value",
        "cur_val",
        "total_cost",
        "investment_value",
        "present_value",
    ],
    "date": [
        "date",
        "trade_date",
        "transaction_date",
        "statement_date",
        "observed_at",
        "timestamp",
        "order_execution_time",
    ],
    "type": [
        "type",
        "txn_type",
        "transaction_type",
        "buy_sell",
        "action",
        "trade_type",
        "side",
    ],
    "exchange": ["exchange", "segment", "mkt"],
    "currency": ["currency", "curr"],
}


@dataclass
class RowError:
    row_number: int
    field: Optional[str]
    raw_value: Any
    reason: str


@dataclass
class ParsedHolding:
    row_number: int
    asset_name: str
    asset_identifier: Optional[str]  # ISIN or Symbol
    symbol: Optional[str]
    isin: Optional[str]
    asset_type: str
    quantity: Decimal
    unit_price: Optional[Decimal]
    market_value: Optional[Decimal]
    currency: str
    observed_at: datetime
    exchange: Optional[str] = "NSE"


@dataclass
class ParsedTransaction:
    row_number: int
    transaction_at: datetime
    transaction_type: str  # 'BUY' or 'SELL'
    quantity: Decimal
    price: Decimal
    amount: Decimal
    currency: str
    symbol: Optional[str]
    isin: Optional[str]
    description: str
    external_transaction_id: Optional[str] = None


@dataclass
class IngestionSummary:
    mode: str  # 'holdings' or 'transactions'
    dry_run: bool
    total_rows: int
    accepted_count: int
    rejected_count: int
    duplicate_count: int
    errors: List[RowError] = field(default_factory=list)
    parsed_items: List[Union[ParsedHolding, ParsedTransaction]] = field(
        default_factory=list
    )


def _normalize_header(name: str) -> str:
    """Strip whitespace, lowercase, remove special characters."""
    return re.sub(r"[^a-z0-9_]", "", name.strip().lower().replace(" ", "_"))


def resolve_column_mappings(
    headers: List[str], custom_aliases: Optional[Dict[str, List[str]]] = None
) -> Dict[str, str]:
    """
    Maps canonical fields to actual CSV header names.
    """
    aliases = {**DEFAULT_COLUMN_ALIASES, **(custom_aliases or {})}
    normalized_headers = {h: _normalize_header(h) for h in headers}

    resolved = {}
    for canonical_field, candidate_names in aliases.items():
        candidates_norm = [_normalize_header(c) for c in candidate_names]
        for header, h_norm in normalized_headers.items():
            if h_norm in candidates_norm or h_norm == canonical_field:
                resolved[canonical_field] = header
                break
    return resolved


def _clean_string(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s not in ("-", "NA", "N/A", "null") else None


def _clean_decimal(val: Any) -> Optional[Decimal]:
    if val is None:
        return None
    s = str(val).strip().replace(",", "").replace("₹", "").replace("$", "")
    # Check for accounting negative format: (123.45) -> -123.45
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()
    if not s or s in ("-", "NA", "N/A"):
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _clean_date(val: Any) -> Optional[date]:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%Y/%m/%d",
        "%d.%m.%Y",
    ):
        try:
            return datetime.strptime(s.split(" ")[0].split("T")[0], fmt).date()
        except ValueError:
            pass
    return None


def _is_valid_isin(s: Optional[str]) -> bool:
    if not s:
        return False
    return bool(re.match(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$", s.upper()))


def _infer_asset_type(symbol: Optional[str], isin: Optional[str], name: Optional[str]) -> str:
    """Classifies asset into EQUITY, MUTUAL_FUND, ETF, or OTHER."""
    isin_upper = (isin or "").upper()
    name_upper = (name or "").upper()

    if isin_upper.startswith("INF") or "MUTUAL FUND" in name_upper or "GROWTH" in name_upper:
        return "MUTUAL_FUND"
    if "ETF" in name_upper or (symbol and symbol.upper().endswith("ETF")):
        return "ETF"
    if isin_upper.startswith("INE"):
        return "EQUITY"
    return "EQUITY"


def parse_holdings_csv(
    csv_text: str,
    statement_date: Optional[date] = None,
    default_currency: str = "INR",
    custom_aliases: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[ParsedHolding], List[RowError], int]:
    """
    Parses holdings snapshot CSV text.
    Returns (accepted_holdings, row_errors, duplicate_count).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return [], [RowError(1, None, None, "CSV has empty header")], 0

    col_map = resolve_column_mappings(reader.fieldnames, custom_aliases)
    accepted: List[ParsedHolding] = []
    errors: List[RowError] = []
    seen_identifiers = set()
    duplicate_count = 0

    observed_dt = (
        datetime.combine(statement_date, datetime.min.time(), tzinfo=timezone.utc)
        if statement_date
        else datetime.now(timezone.utc)
    )

    for idx, row in enumerate(reader, start=2):  # row 1 is header
        clean_row = {k: _clean_string(v) for k, v in row.items() if k}

        raw_sym = clean_row.get(col_map.get("symbol", ""))
        raw_isin = clean_row.get(col_map.get("isin", ""))
        raw_name = clean_row.get(col_map.get("asset_name", ""))
        raw_qty = clean_row.get(col_map.get("quantity", ""))
        raw_price = clean_row.get(col_map.get("price", ""))
        raw_mv = clean_row.get(col_map.get("market_value", ""))
        raw_curr = clean_row.get(col_map.get("currency", ""), default_currency)
        raw_exch = clean_row.get(col_map.get("exchange", ""), "NSE")

        # 1. Identifier check: must have at least symbol, isin, or name
        if not raw_sym and not raw_isin and not raw_name:
            errors.append(RowError(idx, "identifier", None, "Missing symbol, ISIN, and asset name"))
            continue

        isin_val = raw_isin.upper() if raw_isin and _is_valid_isin(raw_isin) else None
        sym_val = raw_sym.upper() if raw_sym else None
        primary_identifier = isin_val or sym_val or raw_name

        # 2. Duplicate detection within the statement
        if primary_identifier in seen_identifiers:
            duplicate_count += 1
            errors.append(
                RowError(
                    idx,
                    "identifier",
                    primary_identifier,
                    f"Duplicate holding row in file for identifier '{primary_identifier}'",
                )
            )
            continue
        seen_identifiers.add(primary_identifier)

        # 3. Quantity check
        qty_dec = _clean_decimal(raw_qty)
        if qty_dec is None or qty_dec < Decimal("0"):
            errors.append(
                RowError(
                    idx, "quantity", raw_qty, f"Invalid or negative quantity: '{raw_qty}'"
                )
            )
            continue

        # 4. Price & Market value check
        price_dec = _clean_decimal(raw_price)
        mv_dec = _clean_decimal(raw_mv)
        if mv_dec is None and price_dec is not None:
            mv_dec = (qty_dec * price_dec).quantize(Decimal("0.0001"))

        asset_name = raw_name or sym_val or isin_val or "UNKNOWN"
        asset_type = _infer_asset_type(sym_val, isin_val, asset_name)

        parsed = ParsedHolding(
            row_number=idx,
            asset_name=asset_name,
            asset_identifier=primary_identifier,
            symbol=sym_val,
            isin=isin_val,
            asset_type=asset_type,
            quantity=qty_dec,
            unit_price=price_dec,
            market_value=mv_dec,
            currency=raw_curr or "INR",
            observed_at=observed_dt,
            exchange=raw_exch or "NSE",
        )
        accepted.append(parsed)

    return accepted, errors, duplicate_count


def parse_transactions_csv(
    csv_text: str,
    default_currency: str = "INR",
    custom_aliases: Optional[Dict[str, List[str]]] = None,
) -> Tuple[List[ParsedTransaction], List[RowError], int]:
    """
    Parses trade transactions ledger CSV text.
    Returns (accepted_transactions, row_errors, duplicate_count).
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        return [], [RowError(1, None, None, "CSV has empty header")], 0

    col_map = resolve_column_mappings(reader.fieldnames, custom_aliases)
    accepted: List[ParsedTransaction] = []
    errors: List[RowError] = []
    seen_txns = set()
    duplicate_count = 0

    for idx, row in enumerate(reader, start=2):
        clean_row = {k: _clean_string(v) for k, v in row.items() if k}

        raw_date = clean_row.get(col_map.get("date", ""))
        raw_type = clean_row.get(col_map.get("type", ""))
        raw_sym = clean_row.get(col_map.get("symbol", ""))
        raw_isin = clean_row.get(col_map.get("isin", ""))
        raw_qty = clean_row.get(col_map.get("quantity", ""))
        raw_price = clean_row.get(col_map.get("price", ""))
        raw_mv = clean_row.get(col_map.get("market_value", ""))
        raw_curr = clean_row.get(col_map.get("currency", ""), default_currency)

        txn_date = _clean_date(raw_date)
        if txn_date is None:
            errors.append(RowError(idx, "date", raw_date, f"Invalid transaction date: '{raw_date}'"))
            continue

        # Normalize BUY / SELL
        side = (raw_type or "").strip().upper()
        if side in ("BUY", "B", "PURCHASE", "DEBIT"):
            norm_side = "BUY"
        elif side in ("SELL", "S", "CREDIT"):
            norm_side = "SELL"
        else:
            errors.append(RowError(idx, "type", raw_type, f"Unknown transaction type: '{raw_type}'"))
            continue

        qty_dec = _clean_decimal(raw_qty)
        if qty_dec is None or qty_dec <= Decimal("0"):
            errors.append(RowError(idx, "quantity", raw_qty, f"Invalid quantity: '{raw_qty}'"))
            continue

        price_dec = _clean_decimal(raw_price)
        if price_dec is None or price_dec <= Decimal("0"):
            errors.append(RowError(idx, "price", raw_price, f"Invalid price: '{raw_price}'"))
            continue

        amount_dec = _clean_decimal(raw_mv) or (qty_dec * price_dec).quantize(Decimal("0.01"))
        sym_val = raw_sym.upper() if raw_sym else None
        isin_val = raw_isin.upper() if raw_isin and _is_valid_isin(raw_isin) else None

        txn_key = (txn_date, sym_val or isin_val, norm_side, qty_dec, price_dec)
        if txn_key in seen_txns:
            duplicate_count += 1
            errors.append(RowError(idx, "transaction", str(txn_key), "Duplicate transaction in file"))
            continue
        seen_txns.add(txn_key)

        txn_dt = datetime.combine(txn_date, datetime.min.time(), tzinfo=timezone.utc)
        desc = f"{norm_side} {qty_dec} {sym_val or isin_val or 'SHARES'} @ {price_dec}"

        accepted.append(
            ParsedTransaction(
                row_number=idx,
                transaction_at=txn_dt,
                transaction_type=norm_side,
                quantity=qty_dec,
                price=price_dec,
                amount=amount_dec,
                currency=raw_curr or "INR",
                symbol=sym_val,
                isin=isin_val,
                description=desc,
            )
        )

    return accepted, errors, duplicate_count


def ingest_demat_holdings(
    session: Session,
    account_id: uuid.UUID,
    csv_text: str,
    statement_date: Optional[date] = None,
    dry_run: bool = False,
    custom_aliases: Optional[Dict[str, List[str]]] = None,
) -> IngestionSummary:
    """
    Ingests demat holdings into investment_holdings table.
    - If dry_run=True, performs full parsing & validation with zero DB writes.
    - Detects database duplicates for (account_id, asset_identifier, observed_at).
    """
    accepted, errors, file_dups = parse_holdings_csv(
        csv_text, statement_date=statement_date, custom_aliases=custom_aliases
    )

    if dry_run or not accepted:
        return IngestionSummary(
            mode="holdings",
            dry_run=dry_run,
            total_rows=len(accepted) + len(errors),
            accepted_count=len(accepted),
            rejected_count=len(errors),
            duplicate_count=file_dups,
            errors=errors,
            parsed_items=accepted,
        )

    # In non-dry-run, write to database
    db_dups = 0
    actually_inserted = 0

    for item in accepted:
        # Check existing holding for same account, identifier and observation time
        existing = session.scalars(
            select(InvestmentHolding.id).where(
                InvestmentHolding.account_id == account_id,
                InvestmentHolding.asset_identifier == item.asset_identifier,
                InvestmentHolding.observed_at == item.observed_at,
            )
        ).first()

        if existing is not None:
            db_dups += 1
            errors.append(
                RowError(
                    item.row_number,
                    "database",
                    item.asset_identifier,
                    f"Holding already recorded for account at {item.observed_at.date()}",
                )
            )
            continue

        holding = InvestmentHolding(
            account_id=account_id,
            asset_name=item.asset_name,
            asset_identifier=item.asset_identifier,
            asset_type=item.asset_type,
            quantity=item.quantity,
            unit_price=item.unit_price,
            market_value=item.market_value,
            currency=item.currency,
            observed_at=item.observed_at,
        )
        session.add(holding)
        actually_inserted += 1

    session.commit()

    return IngestionSummary(
        mode="holdings",
        dry_run=False,
        total_rows=len(accepted) + len(errors),
        accepted_count=actually_inserted,
        rejected_count=len(errors) - db_dups,
        duplicate_count=file_dups + db_dups,
        errors=errors,
        parsed_items=accepted,
    )
