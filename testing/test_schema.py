from pydantic import ValidationError

from app.schemas import NormalizedTransaction

good = {
    "external_transaction_id": "T1",
    "transaction_type": "debit",
    "amount": "250.50",
    "transaction_at": "2026-10-01T10:00:00+05:30",
}
print(NormalizedTransaction(**good))

bad_cases = {
    "no id": {**good, "external_transaction_id": ""},
    "negative amount": {**good, "amount": "-5"},
    "bad type": {**good, "transaction_type": "refund"},
    "no timezone": {**good, "transaction_at": "2026-10-01T10:00:00"},
}

for name, data in bad_cases.items():
    try:
        NormalizedTransaction(**data)
        print(name, "-> ACCEPTED (this is a bug)")
    except ValidationError:
        print(name, "-> rejected")

