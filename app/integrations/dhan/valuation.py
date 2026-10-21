"""Read-only Dhan portfolio valuation from holdings and market quotes."""

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class DhanValuationError(ValueError):
    """Raised when Dhan valuation inputs cannot be reconciled safely."""


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _instrument_segments(instrument_master: str) -> dict[tuple[str, str], set[str]]:
    try:
        reader = csv.DictReader(io.StringIO(instrument_master))
        result: dict[tuple[str, str], set[str]] = {}
        for row in reader:
            if row.get("SEGMENT") != "E":
                continue
            exchange = row.get("EXCH_ID")
            if exchange not in {"NSE", "BSE"}:
                continue
            security_id = (row.get("SECURITY_ID") or "").strip()
            isin = (row.get("ISIN") or "").strip().upper()
            if not security_id or not isin:
                continue
            segment = f"{exchange}_EQ"
            result.setdefault((security_id, isin), set()).add(segment)
    except (csv.Error, UnicodeError) as exc:
        raise DhanValuationError("Dhan instrument master is invalid.") from exc
    return result


def build_live_dhan_valuation(
    holdings: list[dict[str, Any]],
    instrument_master: str,
    quote_payload: dict[str, Any],
) -> dict[str, Any]:
    """Calculate account-level values; never returns individual holding data."""
    segment_map = _instrument_segments(instrument_master)
    quote_data = quote_payload.get("data")
    if not isinstance(quote_data, dict):
        raise DhanValuationError("Dhan market quote response is invalid.")

    normalized: list[tuple[Decimal, Decimal | None, str | None, int | None]] = []
    instruments: dict[str, set[int]] = {}
    for item in holdings:
        quantity = _decimal(item.get("totalQty"))
        if quantity is None or quantity < 0:
            raise DhanValuationError("Dhan holdings response has invalid quantity.")
        if quantity == 0:
            continue

        average_cost = _decimal(item.get("avgCostPrice"))
        raw_security_id = item.get("securityId")
        raw_isin = item.get("isin")
        security_id = str(raw_security_id).strip() if raw_security_id else ""
        isin = str(raw_isin).strip().upper() if raw_isin else ""
        matching_segments = segment_map.get((security_id, isin), set())
        segment = next(iter(matching_segments)) if len(matching_segments) == 1 else None
        numeric_security_id = None
        if segment is not None:
            try:
                numeric_security_id = int(security_id)
            except ValueError:
                segment = None
            else:
                instruments.setdefault(segment, set()).add(numeric_security_id)
        normalized.append((quantity, average_cost, segment, numeric_security_id))

    invested_complete = all(cost is not None for _, cost, _, _ in normalized)
    invested_amount = (
        sum(
            (quantity * cost for quantity, cost, _, _ in normalized if cost is not None),
            Decimal(0),
        )
        if invested_complete
        else None
    )

    quoted_values: list[tuple[Decimal, Decimal]] = []
    for quantity, _, segment, security_id in normalized:
        quote = None
        if segment is not None and security_id is not None:
            segment_quotes = quote_data.get(segment, {})
            if isinstance(segment_quotes, dict):
                quote = segment_quotes.get(str(security_id))
        if not isinstance(quote, dict):
            continue
        last_price = _decimal(quote.get("last_price"))
        net_change = _decimal(quote.get("net_change"))
        if last_price is None or last_price < 0 or net_change is None:
            continue
        quoted_values.append((quantity * last_price, quantity * net_change))

    priced_count = len(quoted_values)
    holdings_count = len(normalized)
    coverage_complete = priced_count == holdings_count
    current_value = (
        sum((market_value for market_value, _ in quoted_values), Decimal(0))
        if coverage_complete
        else None
    )
    today_pnl = (
        sum((daily_change for _, daily_change in quoted_values), Decimal(0))
        if coverage_complete
        else None
    )
    overall_pnl = (
        current_value - invested_amount
        if current_value is not None and invested_amount is not None
        else None
    )

    return {
        "retrieved_at": datetime.now(timezone.utc),
        "holdings_count": len(holdings),
        "priced_count": priced_count,
        "unpriced_count": holdings_count - priced_count,
        "coverage_complete": coverage_complete,
        "invested_amount": invested_amount,
        "current_value": current_value,
        "overall_pnl": overall_pnl,
        "today_pnl": today_pnl,
    }


def fetch_live_dhan_valuation(client: Any) -> dict[str, Any]:
    holdings = client.get_holdings()
    instrument_master = client.get_instrument_master()
    segments = _instrument_segments(instrument_master)
    requested: dict[str, set[int]] = {}
    for item in holdings:
        security_id = str(item.get("securityId") or "").strip()
        isin = str(item.get("isin") or "").strip().upper()
        matches = segments.get((security_id, isin), set())
        if len(matches) != 1:
            continue
        try:
            numeric_security_id = int(security_id)
        except ValueError:
            continue
        requested.setdefault(next(iter(matches)), set()).add(numeric_security_id)

    quote_payload = (
        client.get_market_quote(
            {segment: sorted(security_ids) for segment, security_ids in requested.items()}
        )
        if requested
        else {"data": {}}
    )
    return build_live_dhan_valuation(holdings, instrument_master, quote_payload)
