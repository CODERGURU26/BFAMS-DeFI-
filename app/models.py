import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR, DateTime, ForeignKey, Numeric, Text, UniqueConstraint, Uuid, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

   

class DataProvider(Base):
    __tablename__ = "data_providers"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    provider_code: Mapped[str] = mapped_column(Text)
    provider_name: Mapped[str] = mapped_column(Text)
    provider_type: Mapped[str] = mapped_column(
        Text, server_default=text("'account_aggregator'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

class FinancialInstitution(Base):
    __tablename__ = "financial_institutions"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    institution_name: Mapped[str] = mapped_column(Text)
    institution_code: Mapped[str | None] = mapped_column(Text)
    institution_type: Mapped[str] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(CHAR(2), server_default=text("'IN'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class FinancialConnection(Base):
    __tablename__ = "financial_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("data_providers.id"))
    external_connection_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

class FinancialAccount(Base):
    __tablename__ = "financial_accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("financial_connections.id")
    )
    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("financial_institutions.id")
    )
    external_account_id: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str] = mapped_column(Text)
    account_name: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'INR'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Transaction(Base):
    __tablename__ = "transactions"

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "external_transaction_id",
            name="transactions_account_external_id_key",
        ),
    )


    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("financial_accounts.id"))
    external_transaction_id: Mapped[str | None] = mapped_column(Text)
    transaction_type: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'INR'"))
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    transaction_status: Mapped[str] = mapped_column(
        Text, server_default=text("'unknown'")
    )
    transaction_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

