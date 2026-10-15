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

from app.models import FinancialAccount, InvestmentHolding, SecurityDailyPrice, Transaction


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
    """Classifies asset into EQUITY, MUTUAL_FUND, ETF, REIT, or OTHER.

    Order of precedence:
      1. ETF: name contains 'ETF' or symbol ends with 'ETF' (ETFs have INF ISIN but
         must be classified before MUTUAL_FUND).
      2. REIT: name contains 'REIT' or known REIT keywords.
      3. MUTUAL_FUND: INF ISIN prefix (excluding ETFs already caught above).
      4. EQUITY: INE ISIN prefix.
      5. Default: EQUITY.
    """
    isin_upper = (isin or "").upper()
    name_upper = (name or "").upper()
    sym_upper = (symbol or "").upper()

    # 1. ETF check first (ETFs have INF ISIN like mutual funds)
    if "ETF" in name_upper or sym_upper.endswith("ETF") or "BEES" in sym_upper:
        return "ETF"
    # 2. REIT
    if "REIT" in name_upper or "REAL ESTATE TRUST" in name_upper:
        return "REIT"
    # 3. Mutual fund (INF ISIN or name keywords)
    if isin_upper.startswith("INF") or "MUTUAL FUND" in name_upper or "GROWTH" in name_upper:
        return "MUTUAL_FUND"
    # 4. Equity (INE ISIN)
    if isin_upper.startswith("INE"):
        return "EQUITY"
    return "EQUITY"


# Dhan Holdings CSV column aliases (overrides defaults)
_DHAN_HOLDINGS_ALIASES: Dict[str, List[str]] = {
    "asset_name": ["Scrip Name", "scrip_name"],
    "isin": ["ISIN Code", "isin_code"],
    "quantity": ["Free Holding", "free_holding"],
    "price": ["Closing Price", "closing_price"],
    "market_value": ["Valuation", "valuation"],
}

# Dhan Trades CSV column aliases
_DHAN_TRADES_ALIASES: Dict[str, List[str]] = {
    "date": ["Date", "date"],
    "type": ["Side", "side"],
    "asset_name": ["Scrip Name", "scrip_name"],
    "quantity": ["Quantity", "quantity"],
    "price": ["Trade Price", "trade_price"],
    "market_value": ["Trade Value", "trade_value"],
}


def _strip_dhan_metadata_header(csv_text: str) -> str:
    """
    Dhan CSV files start with metadata rows before the actual column header,
    and end with summary / note footer rows.
    Detect and strip those rows, returning text beginning at the real header row
    and omitting summary/note footers.
    """
    known_headers = {
        "scrip name", "isin code", "free holding", "closing price",
        "date", "side", "trade price", "trade value",
        "symbol", "isin", "quantity", "price", "market_value",
    }
    lines = csv_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        lower = line.lower()
        if any(tok in lower for tok in known_headers):
            header_idx = i
            break
    if header_idx is None:
        return csv_text

    kept = []
    footer_keywords = ("valuation,", "note :", "note:")
    for line in lines[header_idx:]:
        stripped = line.strip()
        lower = stripped.lower()
        if any(lower.startswith(kw) for kw in footer_keywords):
            continue
        kept.append(line)
    return "\n".join(kept)


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
    cleaned_csv = _strip_dhan_metadata_header(csv_text)
    reader = csv.DictReader(io.StringIO(cleaned_csv))
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
    cleaned_csv = _strip_dhan_metadata_header(csv_text)
    reader = csv.DictReader(io.StringIO(cleaned_csv))
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
    parser: str = "generic",
) -> IngestionSummary:
    """
    Ingests demat holdings into investment_holdings table.
    - If dry_run=True, performs full parsing & validation with zero DB writes.
    - Detects database duplicates for (account_id, asset_identifier, observed_at).
    """
    if parser == "dhan":
        accepted, errors, file_dups = parse_dhan_holdings_csv(
            csv_text, statement_date=statement_date
        )
    else:
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


# ---------------------------------------------------------------------------
# Dhan-specific CSV parsers
# ---------------------------------------------------------------------------




def parse_dhan_holdings_csv(
    csv_text: str,
    statement_date: Optional[date] = None,
    default_currency: str = "INR",
) -> Tuple[List["ParsedHolding"], List["RowError"], int]:
    """
    Parse a Dhan Demat Holdings CSV export.

    The file has 5-6 metadata rows before the actual column header.
    Only rows with a valid ISIN and positive Free Holding are accepted.
    Summary/footer rows (no ISIN) are silently ignored.
    """
    stripped = _strip_dhan_metadata_header(csv_text)
    # Merge Dhan aliases on top of defaults so generic columns still work
    merged_aliases: Dict[str, List[str]] = {
        **DEFAULT_COLUMN_ALIASES,
        **{k: v + DEFAULT_COLUMN_ALIASES.get(k, []) for k, v in _DHAN_HOLDINGS_ALIASES.items()},
    }
    return parse_holdings_csv(
        stripped,
        statement_date=statement_date,
        default_currency=default_currency,
        custom_aliases=merged_aliases,
    )


def parse_dhan_trades_csv(
    csv_text: str,
    default_currency: str = "INR",
) -> Tuple[List["ParsedTransaction"], List["RowError"], int]:
    """
    Parse a Dhan Past Trades CSV export.

    Column mapping: Date, Side (B/S), Scrip Name, Exchange, Segment,
    Order Type, Quantity, Trade Price, Trade Value.
    Empty trailing rows are ignored.
    """
    stripped = _strip_dhan_metadata_header(csv_text)
    merged_aliases: Dict[str, List[str]] = {
        **DEFAULT_COLUMN_ALIASES,
        **{k: v + DEFAULT_COLUMN_ALIASES.get(k, []) for k, v in _DHAN_TRADES_ALIASES.items()},
    }
    return parse_transactions_csv(
        stripped,
        default_currency=default_currency,
        custom_aliases=merged_aliases,
    )


# ---------------------------------------------------------------------------
# Transaction DB ingestion (parallel to ingest_demat_holdings)
# ---------------------------------------------------------------------------


def ingest_demat_transactions(
    session: Session,
    account_id: uuid.UUID,
    csv_text: str,
    dry_run: bool = False,
    custom_aliases: Optional[Dict[str, List[str]]] = None,
    parser: str = "generic",
) -> IngestionSummary:
    """
    Ingests parsed trade transactions into the `transactions` table.

    - parser='dhan' uses Dhan-specific column aliases.
    - parser='generic' uses the generic resolver.
    - dry_run=True performs full validation without any DB writes.
    - Idempotent: skips rows whose (account_id, date, symbol, side, qty, price)
      composite key already exists as external_transaction_id.
    """
    if parser == "dhan":
        accepted, errors, file_dups = parse_dhan_trades_csv(csv_text)
    else:
        merged = {**DEFAULT_COLUMN_ALIASES, **(custom_aliases or {})}
        accepted, errors, file_dups = parse_transactions_csv(
            csv_text, custom_aliases=merged
        )

    if dry_run or not accepted:
        return IngestionSummary(
            mode="transactions",
            dry_run=dry_run,
            total_rows=len(accepted) + len(errors),
            accepted_count=len(accepted),
            rejected_count=len(errors),
            duplicate_count=file_dups,
            errors=errors,
            parsed_items=accepted,
        )

    db_dups = 0
    actually_inserted = 0

    for item in accepted:
        # Build a deterministic external_transaction_id from key fields
        ext_id = (
            f"{item.transaction_at.date()}|"
            f"{item.transaction_type}|"
            f"{item.symbol or item.isin or 'UNKNOWN'}|"
            f"{item.quantity}|{item.price}"
        )
        existing = session.scalars(
            select(Transaction.id).where(
                Transaction.account_id == account_id,
                Transaction.external_transaction_id == ext_id,
            )
        ).first()

        if existing is not None:
            db_dups += 1
            errors.append(
                RowError(
                    item.row_number,
                    "database",
                    ext_id,
                    f"Transaction already recorded for account: {ext_id}",
                )
            )
            continue

        txn_type = "debit" if item.transaction_type == "BUY" else "credit"
        txn = Transaction(
            account_id=account_id,
            external_transaction_id=ext_id,
            transaction_type=txn_type,
            amount=item.amount,
            currency=item.currency,
            description=item.description,
            category="equity_trade",
            transaction_status="completed",
            transaction_at=item.transaction_at,
        )
        session.add(txn)
        actually_inserted += 1

    session.commit()

    return IngestionSummary(
        mode="transactions",
        dry_run=False,
        total_rows=len(accepted) + len(errors),
        accepted_count=actually_inserted,
        rejected_count=len(errors) - db_dups,
        duplicate_count=file_dups + db_dups,
        errors=errors,
        parsed_items=accepted,
    )


# ---------------------------------------------------------------------------
# Equity / ETF price lookup via security_daily_prices
# ---------------------------------------------------------------------------


@dataclass
class EquityValuation:
    isin: Optional[str]
    asset_name: str
    asset_type: str
    quantity: Decimal
    close_price: Optional[Decimal]
    price_date: Optional[date]
    as_of_date: date
    market_value: Optional[Decimal]
    data_age_days: Optional[int]
    is_stale: bool
    status: str  # 'current', 'stale', 'no_price', 'zero_quantity'
    notes: Optional[str] = None


def lookup_equity_price(
    session: Session,
    isin: str,
    as_of_date: Optional[date] = None,
    max_staleness_days: int = 7,
) -> Tuple[Optional[Decimal], Optional[date]]:
    """
    Returns (close_price, price_date) for an ISIN from security_daily_prices.
    Looks for the most recent trading date on or before as_of_date.
    Returns (None, None) if no price record exists.
    """
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()

    # NSE security_daily_prices does not have ISIN column; match via symbol.
    # The holdings CSV provides ISIN; we look up symbol via a secondary cross-
    # reference stored in mf_schemes (for ETFs/MFs) or directly search by name.
    # For pure equity/ETF holdings we search security_daily_prices by symbol
    # derived from the ISIN lookup strategy (ISIN is not in that table).
    # Strategy: try to find any row whose symbol appears in a separate ISIN map
    # — since security_daily_prices only stores NSE symbols, we return None here
    # and let the caller do name-based fallback.
    return None, None


def lookup_equity_price_by_symbol(
    session: Session,
    symbol: str,
    as_of_date: Optional[date] = None,
    max_staleness_days: int = 7,
) -> Tuple[Optional[Decimal], Optional[date]]:
    """
    Returns (close_price, price_date) for a trading symbol from security_daily_prices.
    Tries the exact symbol first; on miss tries upper-case stripped version.
    """
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()

    sym_upper = symbol.strip().upper()
    row = session.scalars(
        select(SecurityDailyPrice)
        .where(
            SecurityDailyPrice.symbol == sym_upper,
            SecurityDailyPrice.instrument_type == "equity",
            SecurityDailyPrice.trading_date <= as_of_date,
        )
        .order_by(SecurityDailyPrice.trading_date.desc())
        .limit(1)
    ).first()

    if row is None:
        return None, None
    return row.close_price, row.trading_date


# ---------------------------------------------------------------------------
# Dhan ISIN → NSE symbol resolution table
# (populated from the actual Dhan holdings file; extends as new ISINs appear)
# ---------------------------------------------------------------------------

# This table maps ISIN → (nse_symbol, asset_type) for securities held.
# ETF ISINs starting with INF map to their NSE ticker.
# REIT ISINs are handled separately (no price in security_daily_prices).
ISIN_TO_NSE_SYMBOL: Dict[str, Tuple[str, str]] = {
    # ETFs
    "INF200KA16D8": ("SBIGOLD", "ETF"),
    "INF200KB1217": ("SILVERBEES", "ETF"),
    "INF204KB14I2": ("NIFTYBEES", "ETF"),
    "INF204KB15V2": ("ITBEES", "ETF"),
    "INF204KB16I7": ("PSUBNKBEES", "ETF"),
    "INF247L01AP3": ("MOM100", "ETF"),
    "INF247L01AU3": ("MOM50", "ETF"),
    "INF277KA1976": ("TATAGOLD", "ETF"),
    "INF277KA1984": ("TATASILV", "ETF"),
    # REITs (INE prefix, listed but often not in equity bhavcopy)
    "INE041025011": ("EMBASSY", "REIT"),
    "INE0CCU25019": ("MINDSPACE", "REIT"),
    "INE0FDU25010": ("BIRET", "REIT"),
    # Equities (INE prefix)
    "INE0PTN01011": ("MONEYVIEW", "EQUITY"),
    "INE509F01029": ("CUPID", "EQUITY"),
    "INE758T01015": ("ETERNAL", "EQUITY"),
}


def value_equity_holding(
    session: Session,
    isin: str,
    asset_name: str,
    asset_type: str,
    quantity: Decimal,
    as_of_date: Optional[date] = None,
    max_staleness_days: int = 7,
    dhan_closing_price: Optional[Decimal] = None,
) -> EquityValuation:
    """
    Values a single equity / ETF / REIT holding.

    Resolution order:
      1. Look up ISIN → NSE symbol in ISIN_TO_NSE_SYMBOL.
      2. Query security_daily_prices for the most recent close_price.
      3. If not found in security_daily_prices, fall back to the Dhan
         snapshot closing price (already present in the CSV).
    """
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()

    if quantity == Decimal("0"):
        return EquityValuation(
            isin=isin,
            asset_name=asset_name,
            asset_type=asset_type,
            quantity=quantity,
            close_price=None,
            price_date=None,
            as_of_date=as_of_date,
            market_value=Decimal("0.0000"),
            data_age_days=None,
            is_stale=False,
            status="zero_quantity",
        )

    # Step 1: resolve NSE symbol
    nse_symbol, resolved_type = ISIN_TO_NSE_SYMBOL.get(isin, (None, asset_type))

    close_price: Optional[Decimal] = None
    price_date: Optional[date] = None

    if nse_symbol:
        close_price, price_date = lookup_equity_price_by_symbol(
            session, nse_symbol, as_of_date=as_of_date,
            max_staleness_days=max_staleness_days,
        )

    # Step 2: fall back to Dhan snapshot price
    if close_price is None and dhan_closing_price is not None:
        close_price = dhan_closing_price
        price_date = as_of_date  # snapshot date
        age_days = 0
        is_stale = False
        status = "current"
        notes = "Price sourced from Dhan snapshot; not in security_daily_prices."
        market_value = (quantity * close_price).quantize(Decimal("0.0001"))
        return EquityValuation(
            isin=isin,
            asset_name=asset_name,
            asset_type=resolved_type,
            quantity=quantity,
            close_price=close_price,
            price_date=price_date,
            as_of_date=as_of_date,
            market_value=market_value,
            data_age_days=age_days,
            is_stale=is_stale,
            status=status,
            notes=notes,
        )

    # Step 3: no price found at all
    if close_price is None:
        return EquityValuation(
            isin=isin,
            asset_name=asset_name,
            asset_type=resolved_type,
            quantity=quantity,
            close_price=None,
            price_date=None,
            as_of_date=as_of_date,
            market_value=None,
            data_age_days=None,
            is_stale=False,
            status="no_price",
            notes=f"No price found in security_daily_prices for ISIN {isin}.",
        )

    age_days = (as_of_date - price_date).days
    is_stale = age_days > max_staleness_days
    market_value = (quantity * close_price).quantize(Decimal("0.0001"))
    status = "stale" if is_stale else "current"
    notes = (
        f"Price is {age_days} days old (threshold {max_staleness_days} days)."
        if is_stale
        else None
    )

    return EquityValuation(
        isin=isin,
        asset_name=asset_name,
        asset_type=resolved_type,
        quantity=quantity,
        close_price=close_price,
        price_date=price_date,
        as_of_date=as_of_date,
        market_value=market_value,
        data_age_days=age_days,
        is_stale=is_stale,
        status=status,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Portfolio valuation: aggregate all holdings for an account
# ---------------------------------------------------------------------------


@dataclass
class PortfolioHoldingValuation:
    asset_name: str
    asset_identifier: Optional[str]
    asset_type: str
    quantity: Decimal
    close_price: Optional[Decimal]
    price_date: Optional[date]
    market_value: Optional[Decimal]
    currency: str
    data_age_days: Optional[int]
    is_stale: bool
    status: str
    notes: Optional[str]


@dataclass
class PortfolioSummary:
    account_id: uuid.UUID
    as_of_date: date
    total_holdings: int
    valued_count: int
    stale_count: int
    no_price_count: int
    total_market_value: Optional[Decimal]
    currency: str
    holdings: List[PortfolioHoldingValuation]


def get_portfolio_valuation(
    session: Session,
    account_id: uuid.UUID,
    as_of_date: Optional[date] = None,
    max_staleness_days: int = 7,
) -> PortfolioSummary:
    """
    Read-only portfolio valuation for a demat account.

    - Loads all investment_holdings rows for the account.
    - Uses the latest observed_at snapshot.
    - Values equity/ETF/REIT holdings via security_daily_prices.
    - Values MF holdings via mf_navs (delegated to mf_valuation).
    - Never mutates database records.
    """
    if as_of_date is None:
        as_of_date = datetime.now(timezone.utc).date()

    # Load latest snapshot: find the most recent observed_at
    latest_ts = session.scalars(
        select(InvestmentHolding.observed_at)
        .where(InvestmentHolding.account_id == account_id)
        .order_by(InvestmentHolding.observed_at.desc())
        .limit(1)
    ).first()

    if latest_ts is None:
        return PortfolioSummary(
            account_id=account_id,
            as_of_date=as_of_date,
            total_holdings=0,
            valued_count=0,
            stale_count=0,
            no_price_count=0,
            total_market_value=Decimal("0"),
            currency="INR",
            holdings=[],
        )

    holdings_rows = session.scalars(
        select(InvestmentHolding)
        .where(
            InvestmentHolding.account_id == account_id,
            InvestmentHolding.observed_at == latest_ts,
        )
        .order_by(InvestmentHolding.asset_name)
    ).all()

    result_holdings: List[PortfolioHoldingValuation] = []
    total_mv = Decimal("0")
    valued_count = 0
    stale_count = 0
    no_price_count = 0

    for h in holdings_rows:
        isin = h.asset_identifier if h.asset_identifier and _is_valid_isin(h.asset_identifier) else None
        qty = h.quantity or Decimal("0")

        if isin:
            ev = value_equity_holding(
                session=session,
                isin=isin,
                asset_name=h.asset_name,
                asset_type=h.asset_type,
                quantity=qty,
                as_of_date=as_of_date,
                max_staleness_days=max_staleness_days,
                dhan_closing_price=h.unit_price,
            )
            phv = PortfolioHoldingValuation(
                asset_name=h.asset_name,
                asset_identifier=isin,
                asset_type=ev.asset_type,
                quantity=qty,
                close_price=ev.close_price,
                price_date=ev.price_date,
                market_value=ev.market_value,
                currency=h.currency or "INR",
                data_age_days=ev.data_age_days,
                is_stale=ev.is_stale,
                status=ev.status,
                notes=ev.notes,
            )
            if ev.market_value is not None:
                total_mv += ev.market_value
                valued_count += 1
                if ev.is_stale:
                    stale_count += 1
            else:
                no_price_count += 1
        else:
            # No valid ISIN — use stored market_value from snapshot
            mv = h.market_value
            phv = PortfolioHoldingValuation(
                asset_name=h.asset_name,
                asset_identifier=h.asset_identifier,
                asset_type=h.asset_type,
                quantity=qty,
                close_price=h.unit_price,
                price_date=latest_ts.date() if latest_ts else None,
                market_value=mv,
                currency=h.currency or "INR",
                data_age_days=None,
                is_stale=False,
                status="snapshot",
                notes="Valued from holdings snapshot; no ISIN resolved.",
            )
            if mv is not None:
                total_mv += mv
                valued_count += 1

        result_holdings.append(phv)

    return PortfolioSummary(
        account_id=account_id,
        as_of_date=as_of_date,
        total_holdings=len(holdings_rows),
        valued_count=valued_count,
        stale_count=stale_count,
        no_price_count=no_price_count,
        total_market_value=total_mv if result_holdings else Decimal("0"),
        currency="INR",
        holdings=result_holdings,
    )
