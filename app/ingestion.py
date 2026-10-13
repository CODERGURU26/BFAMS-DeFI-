from pydantic import ValidationError

from app.schemas import NormalizedAccount, NormalizedTransaction


def validate_accounts(raw_accounts: list[dict]):
    valid, rejected = [], []
    for index, raw in enumerate(raw_accounts):
        try:
            valid.append(NormalizedAccount(**raw))
        except ValidationError as exc:
            rejected.append({"index": index, "errors": exc.errors(include_input=False)})
    return valid, rejected


def validate_transactions(raw_transactions: list[dict]):
    valid, rejected = [], []
    seen_ids = set()
    for index, raw in enumerate(raw_transactions):
        try:
            txn = NormalizedTransaction(**raw)
        except ValidationError as exc:
            rejected.append({"index": index, "errors": exc.errors(include_input=False)})
            continue
        if txn.external_transaction_id in seen_ids:
            rejected.append({"index": index, "errors": "duplicate external_transaction_id in this batch"})
            continue
        seen_ids.add(txn.external_transaction_id)
        valid.append(txn)
    return valid, rejected