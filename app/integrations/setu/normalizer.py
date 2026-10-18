"""Normalize supported Setu deposit FI records into existing schemas."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from app.integrations.setu.parser import parse_setu_fi_response
from app.schemas import NormalizedAccount, NormalizedTransaction

_ACCOUNT_STATUS_MAP = {
    "ACTIVE": "active",
    "INACTIVE": "inactive",
    "CLOSED": "closed",
}

_TRANSACTION_STATUS_MAP = {
    "PENDING": "pending",
    "COMPLETED": "completed",
    "SUCCESS": "completed",
    "FAILED": "failed",
    "REVERSED": "reversed",
}
_SUPPORTED_ACCOUNT_TYPES = {"deposit"}


def _reject(index: int, code: str) -> dict[str, Any]:
    return {"index": index, "reason": code}


def _parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("naive timestamp")
    return parsed


def normalize_setu_fi_response(
    payload: dict[str, Any],
) -> tuple[
    list[NormalizedAccount],
    dict[str, list[NormalizedTransaction]],
    list[dict[str, Any]],
]:
    parsed_accounts = parse_setu_fi_response(payload)
    accounts = []
    transactions_by_account: dict[str, list[NormalizedTransaction]] = defaultdict(list)
    rejected = []
    seen_accounts = set()

    for account_index, parsed_account in enumerate(parsed_accounts):
        external_account_id = parsed_account.account_id
        if not isinstance(external_account_id, str) or not external_account_id.strip():
            rejected.append(_reject(account_index, "missing_stable_account_id"))
            continue
        external_account_id = external_account_id.strip()
        if external_account_id in seen_accounts:
            rejected.append(_reject(account_index, "duplicate_account_id"))
            continue

        account_type = parsed_account.account_type
        if not isinstance(account_type, str) or not account_type.strip():
            rejected.append(_reject(account_index, "missing_account_type"))
            continue
        account_type = account_type.strip().lower()
        if account_type not in _SUPPORTED_ACCOUNT_TYPES:
            rejected.append(_reject(account_index, "unsupported_account_type"))
            continue

        status = _ACCOUNT_STATUS_MAP.get(
            str(parsed_account.account_status).upper(),
            "unknown",
        )
        try:
            normalized_account = NormalizedAccount(
                external_account_id=external_account_id,
                account_type=account_type,
                currency=parsed_account.currency or "INR",
                status=status,
            )
        except ValidationError:
            rejected.append(_reject(account_index, "invalid_account"))
            continue

        seen_accounts.add(external_account_id)
        accounts.append(normalized_account)
        seen_transactions = set()
        for transaction_index, transaction in enumerate(parsed_account.transactions):
            try:
                transaction_id = transaction.transaction_id
                if not isinstance(transaction_id, str) or not transaction_id.strip():
                    raise ValueError("missing transaction id")
                transaction_id = transaction_id.strip()
                if transaction_id in seen_transactions:
                    rejected.append(
                        _reject(
                            transaction_index,
                            "duplicate_transaction_id",
                        )
                    )
                    continue

                transaction_type = str(transaction.transaction_type).upper()
                if transaction_type not in {"CREDIT", "DEBIT"}:
                    raise ValueError("unsupported transaction type")

                try:
                    amount = Decimal(str(transaction.amount))
                except (InvalidOperation, ValueError):
                    raise ValueError("invalid amount") from None
                if not amount.is_finite() or amount <= 0:
                    raise ValueError("invalid amount")

                transaction_at = _parse_timestamp(transaction.transaction_at)
                transaction_status = _TRANSACTION_STATUS_MAP.get(
                    str(transaction.status).upper(),
                    "unknown",
                )
                currency = (
                    transaction.currency
                    or parsed_account.currency
                    or "INR"
                )
                if not isinstance(currency, str):
                    raise ValueError("invalid currency")
                normalized_transaction = NormalizedTransaction(
                    external_transaction_id=transaction_id,
                    transaction_type=transaction_type.lower(),
                    amount=amount,
                    currency=currency.upper(),
                    description=transaction.description
                    if isinstance(transaction.description, str)
                    else None,
                    transaction_status=transaction_status,
                    transaction_at=transaction_at,
                )
            except (ValueError, ValidationError):
                rejected.append(_reject(transaction_index, "invalid_transaction"))
                continue

            seen_transactions.add(transaction_id)
            transactions_by_account[external_account_id].append(
                normalized_transaction
            )

    return accounts, dict(transactions_by_account), rejected
