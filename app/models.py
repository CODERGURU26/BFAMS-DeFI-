import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
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


class ConsentRecord(Base):
    __tablename__ = "consent_records"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("financial_connections.id")
    )
    external_consent_id: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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


class DataSyncRun(Base):
    __tablename__ = "data_sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("financial_connections.id")
    )
    sync_type: Mapped[str] = mapped_column(Text, server_default=text("'full'"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    records_processed: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MfScheme(Base):
    __tablename__ = "mf_schemes"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    scheme_code: Mapped[str] = mapped_column(Text, unique=True)
    isin_primary: Mapped[str | None] = mapped_column(Text)
    isin_reinvestment: Mapped[str | None] = mapped_column(Text)
    scheme_name: Mapped[str] = mapped_column(Text)
    plan: Mapped[str | None] = mapped_column(Text)
    option_name: Mapped[str | None] = mapped_column(Text)
    amc_name: Mapped[str] = mapped_column(Text)
    scheme_type: Mapped[str] = mapped_column(Text)
    scheme_category: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MfNav(Base):
    __tablename__ = "mf_navs"

    scheme_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("mf_schemes.id"), primary_key=True
    )
    nav_date: Mapped[date] = mapped_column(Date, primary_key=True)
    nav: Mapped[Decimal] = mapped_column(Numeric(24, 10))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MarketDataLoad(Base):
    __tablename__ = "market_data_loads"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    source: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'running'"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rows_loaded: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_rejected: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class InvestmentHolding(Base):
    __tablename__ = "investment_holdings"

    __table_args__ = (
        Index(
            "idx_investment_holdings_account_observed",
            "account_id",
            "observed_at",
        ),
        Index(
            "idx_investment_holdings_mf_scheme",
            "mf_scheme_id",
            postgresql_where=text("mf_scheme_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("financial_accounts.id")
    )
    mf_scheme_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("mf_schemes.id"), nullable=True
    )
    asset_name: Mapped[str] = mapped_column(Text)
    asset_identifier: Mapped[str | None] = mapped_column(Text)
    asset_type: Mapped[str] = mapped_column(Text)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    market_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    currency: Mapped[str] = mapped_column(CHAR(3), server_default=text("'INR'"))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SecurityDailyPrice(Base):
    __tablename__ = "security_daily_prices"

    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "trading_date",
            "instrument_type",
            name="uq_security_daily_prices_symbol_date_type",
        ),
        Index("idx_security_daily_prices_sym_date", "symbol", "trading_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    symbol: Mapped[str] = mapped_column(Text)
    series: Mapped[str | None] = mapped_column(Text, nullable=True)
    instrument_type: Mapped[str] = mapped_column(Text)  # 'equity' or 'index'
    exchange: Mapped[str] = mapped_column(Text, server_default=text("'NSE'"))
    trading_date: Mapped[date] = mapped_column(Date)
    open_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    high_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    low_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    close_price: Mapped[Decimal] = mapped_column(Numeric(16, 4))
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    prev_close: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    turnover_lacs: Mapped[Decimal | None] = mapped_column(Numeric(16, 4))
    source: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserCredential(Base):
    __tablename__ = "user_credentials"

    __table_args__ = (
        CheckConstraint("email = lower(email)", name="user_credentials_email_lower"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), primary_key=True
    )
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    failed_attempts: Mapped[int] = mapped_column(Integer)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_active: Mapped[bool] = mapped_column()
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    __table_args__ = (
        Index("idx_refresh_tokens_family", "family_id"),
        Index("idx_refresh_tokens_user", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id"), nullable=True
    )


class AuthEvent(Base):
    __tablename__ = "auth_events"

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('register', 'login_success', 'login_failed', 'locked', "
            "'refresh', 'refresh_reuse_detected', 'logout', 'password_changed')",
            name="auth_events_event_type_check",
        ),
        Index(
            "idx_auth_events_user_time",
            "user_id",
            text("occurred_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid, primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )