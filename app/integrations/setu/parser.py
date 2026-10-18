"""Strict parsing for Setu FI data-session JSON responses."""

from dataclasses import dataclass
from typing import Any

from app.integrations.setu.exceptions import SetuMalformedResponseError


@dataclass(frozen=True)
class ParsedSetuTransaction:
    transaction_id: Any
    transaction_type: Any
    amount: Any
    currency: Any
    description: Any
    transaction_at: Any
    status: Any


@dataclass(frozen=True)
class ParsedSetuAccount:
    account_id: Any
    account_type: Any
    account_status: Any
    currency: Any
    transactions: tuple[ParsedSetuTransaction, ...]


def _object(value: Any) -> bool:
    return isinstance(value, dict)


def parse_setu_fi_response(
    payload: dict[str, Any],
) -> list[ParsedSetuAccount]:
    fips = payload.get("fips")
    if not isinstance(fips, list):
        raise SetuMalformedResponseError("Setu financial data response is invalid.")

    parsed_accounts = []
    for fip in fips:
        if not _object(fip) or not isinstance(fip.get("accounts"), list):
            raise SetuMalformedResponseError("Setu financial data response is invalid.")

        for account_record in fip["accounts"]:
            if not _object(account_record):
                raise SetuMalformedResponseError(
                    "Setu financial data response is invalid."
                )
            data = account_record.get("data")
            if data is None:
                continue
            if not _object(data):
                raise SetuMalformedResponseError(
                    "Setu financial data response is invalid."
                )

            decrypted = data.get("decryptedFI")
            if decrypted is not None:
                if not _object(decrypted):
                    raise SetuMalformedResponseError(
                        "Setu financial data response is invalid."
                    )
                data = decrypted

            account = data.get("account")
            if not _object(account):
                raise SetuMalformedResponseError(
                    "Setu financial data response is invalid."
                )

            transactions_data = account.get("transactions")
            transactions = []
            if transactions_data is not None:
                if not _object(transactions_data):
                    raise SetuMalformedResponseError(
                        "Setu financial data response is invalid."
                    )
                raw_transactions = transactions_data.get("transaction", [])
                if not isinstance(raw_transactions, list):
                    raise SetuMalformedResponseError(
                        "Setu financial data response is invalid."
                    )
                for raw_transaction in raw_transactions:
                    if not _object(raw_transaction):
                        raise SetuMalformedResponseError(
                            "Setu financial data response is invalid."
                        )
                    transactions.append(
                        ParsedSetuTransaction(
                            transaction_id=raw_transaction.get("txnId"),
                            transaction_type=raw_transaction.get("type"),
                            amount=raw_transaction.get("amount"),
                            currency=raw_transaction.get("currency"),
                            description=raw_transaction.get("narration"),
                            transaction_at=raw_transaction.get(
                                "transactionTimestamp"
                            ),
                            status=raw_transaction.get("status"),
                        )
                    )

            summary = account.get("summary")
            if summary is not None and not _object(summary):
                raise SetuMalformedResponseError(
                    "Setu financial data response is invalid."
                )
            summary = summary or {}
            parsed_accounts.append(
                ParsedSetuAccount(
                    account_id=(
                        account.get("linkedAccRef")
                        or account_record.get("linkRefNumber")
                    ),
                    account_type=account.get("type"),
                    account_status=summary.get("status"),
                    currency=summary.get("currency"),
                    transactions=tuple(transactions),
                )
            )

    return parsed_accounts
