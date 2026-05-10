"""walking skeleton

Revision ID: 0001
Revises:
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_kind", sa.Text, nullable=False),
        sa.Column("source_account_id", sa.Text, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "source_kind", "source_account_id", name="uq_accounts_source"
        ),
    )
    op.create_table(
        "transactions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.BigInteger,
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_tx_id", sa.Text, nullable=False),
        sa.Column("amount_minor", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("time", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("raw_payload", postgresql.JSONB, nullable=False),
        sa.Column(
            "is_deleted", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        # Forward-looking columns (Phase 1 doesn't read; later phases do)
        sa.Column("hold", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("category_id", sa.BigInteger, nullable=True),
        sa.Column("category_source", sa.Text, nullable=True),
        sa.Column(
            "is_user_locked", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("mcc", sa.Integer, nullable=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("attributed_day", sa.Date, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Composite idempotency key — partial unique index (ING-04 + ING-07 groundwork)
    op.create_index(
        "uq_transactions_account_source_tx",
        "transactions",
        ["account_id", "source_tx_id"],
        unique=True,
        postgresql_where=sa.text("NOT is_deleted"),
    )
    # Rate-limit gate state (ING-02)
    op.create_table(
        "mono_rate_state",
        sa.Column("token_hash", sa.Text, primary_key=True),
        sa.Column("last_acquired_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mono_rate_state")
    op.drop_index("uq_transactions_account_source_tx", table_name="transactions")
    op.drop_table("transactions")
    op.drop_table("accounts")
