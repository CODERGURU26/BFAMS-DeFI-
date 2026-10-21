"""Read-only Dhan portfolio and open-position summaries."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class DhanValuationError(ValueError):
    """Raised when Dhan portfolio data cannot be reconciled safely."""


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _position_pnl(item: dict[str, Any], preferred: str, documented: str) -> Decimal | None:
    value = item.get(preferred)
    if value is None:
        value = item.get(documented)
    return _decimal(value)


def _sum_position_pnl(
    positions: list[dict[str, Any]],
    preferred: str,
    documented: str | None = None,
) -> Decimal | None:
    if not positions:
        return Decimal(0)
    values = [
        _position_pnl(item, preferred, documented or preferred)
        for item in positions
    ]
    if any(value is None for value in values):
        return None
    return sum((value for value in values if value is not None), Decimal(0))


def _open_position(item: dict[str, Any]) -> dict[str, Any]:
    quantity = item.get("netQty")
    if quantity is None:
        quantity = item.get("netQuantity")
    position_type = item.get("positionType")
    is_open = _decimal(quantity)
    if (is_open is not None and is_open == 0) or str(position_type).upper() == "CLOSED":
        return {}

    return {
        "trading_symbol": item.get("tradingSymbol"),
        "position_type": position_type,
        "product_type": item.get("productType"),
        "net_quantity": str(quantity) if quantity is not None else None,
        "realized_pnl": _string_value(
            _position_pnl(item, "realizedPnl", "realizedProfit")
        ),
        "unrealized_pnl": _string_value(
            _position_pnl(item, "unrealizedPnl", "unrealizedProfit")
        ),
        "day_pnl": _string_value(_decimal(item.get("dayPnl"))),
    }


def _string_value(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def build_live_dhan_portfolio(
    holdings: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
) -> dict[str, Any]:
    """Summarize Dhan API data without deriving P&L from market prices."""
    if not all(isinstance(row, dict) for row in holdings + positions + orders):
        raise DhanValuationError("Dhan portfolio response is invalid.")

    open_positions = [
        row
        for item in positions
        if (row := _open_position(item))
    ]
    return {
        "retrieved_at": datetime.now(timezone.utc),
        "holdings_count": len(holdings),
        "position_count": len(positions),
        "orders_count": len(orders),
        "realized_pnl": _sum_position_pnl(
            positions,
            "realizedPnl",
            "realizedProfit",
        ),
        "unrealized_pnl": _sum_position_pnl(
            positions,
            "unrealizedPnl",
            "unrealizedProfit",
        ),
        "day_pnl": _sum_position_pnl(positions, "dayPnl"),
        "open_positions": open_positions,
    }


def fetch_live_dhan_portfolio(client: Any) -> dict[str, Any]:
    """Fetch holdings, positions, and today's order book without writing."""
    holdings = client.get_holdings()
    positions = client.get_positions()
    orders = client.get_orders()
    return build_live_dhan_portfolio(holdings, positions, orders)
