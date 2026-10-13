from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

TransactionType = Literal["credit", "debit", "other"]
TransactionStatus = Literal["pending", "completed", "failed", "reversed", "unknown"]
AccountStatus = Literal["active", "inactive", "closed", "unknown"]


class NormalizedAccount(BaseModel):
    external_account_id: str = Field(min_length=1)
    account_type: str = Field(min_length=1)
    account_name: str | None = None
    currency: str = Field(default="INR", min_length=3, max_length=3)
    status: AccountStatus = "unknown"
    source_updated_at: datetime | None = None


class NormalizedTransaction(BaseModel):
    external_transaction_id: str = Field(min_length=1)
    transaction_type: TransactionType
    amount: Decimal = Field(gt=0, max_digits=16, decimal_places=4)
    currency: str = Field(default="INR", min_length=3, max_length=3)
    description: str | None = None
    category: str | None = None
    transaction_status: TransactionStatus = "unknown"
    transaction_at: datetime
    source_updated_at: datetime | None = None

    @field_validator("transaction_at")
    @classmethod
    def must_have_timezone(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("transaction_at must include a timezone")
        return v