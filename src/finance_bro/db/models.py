from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    TIMESTAMP,
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_kind: Mapped[str] = mapped_column(Text, nullable=False)
    source_account_id: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    mono_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("source_kind", "source_account_id", name="uq_accounts_source"),
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_tx_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    time: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Forward-looking — Phase 1 never reads, later phases retrofit-painfully
    hold: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    category_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    category_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_user_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    mcc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributed_day: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index(
            "uq_transactions_account_source_tx",
            "account_id",
            "source_tx_id",
            unique=True,
            postgresql_where=text("NOT is_deleted"),
        ),
    )


class MonoRateState(Base):
    __tablename__ = "mono_rate_state"

    token_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    last_acquired_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)


class ImportRun(Base):
    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_kind: Mapped[str] = mapped_column(Text, nullable=False)
    window_from: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    window_to: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    statement_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inserted: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class SchedulerState(Base):
    __tablename__ = "scheduler_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'running'")
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    since: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class FxRate(Base):
    """NBU daily FX rate, keyed (rate_date, currency) — FX-02 / D-04.

    Rates are computed on read via a LATERAL join (FX-03); this table is the
    sole source of truth and is NEVER denormalized into transactions.
    """

    __tablename__ = "fx_rates"

    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        PrimaryKeyConstraint("rate_date", "currency"),
        # D-04 covering index for the DESC LATERAL lookup (leading column
        # currency, then rate_date — Postgres scans backward for the DESC limit).
        Index("ix_fx_rates_currency_rate_date", "currency", "rate_date"),
    )


class TrackedFxCurrency(Base):
    """Currencies whose NBU rates we fetch — D-05.

    Seeded with USD/EUR; new currencies are lazily inserted on first sighting
    (D-15). `bootstrap_done` flips true only after the 12-month backfill lands.
    """

    __tablename__ = "tracked_fx_currencies"

    currency: Mapped[str] = mapped_column(CHAR(3), primary_key=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
    bootstrap_done: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
