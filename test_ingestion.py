from app.ingestion import validate_accounts, validate_transactions

accounts = [
    {"external_account_id": "A1", "account_type": "savings", "status": "active"},
    {"external_account_id": "", "account_type": "savings"},
    {"external_account_id": "A3", "account_type": "savings", "currency": "RUPEE"},
    {"external_account_id": "A4", "account_type": "savings", "status": "frozen"},
]
valid, rejected = validate_accounts(accounts)
print("accounts valid:", len(valid), "rejected:", [r["index"] for r in rejected])

base = {
    "transaction_type": "credit",
    "amount": "100.00",
    "transaction_at": "2026-10-01T10:00:00+05:30",
}
transactions = [
    {**base, "external_transaction_id": "T1"},
    {**base, "external_transaction_id": "T1"},
    {**base, "external_transaction_id": "T3", "amount": "0"},
    {**base, "external_transaction_id": "T4", "amount": "12.50"},
]
valid, rejected = validate_transactions(transactions)
print("transactions valid:", len(valid), "rejected:", [r["index"] for r in rejected])