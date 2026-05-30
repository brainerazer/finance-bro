"""fx truth — fx_rates, tracked_fx_currencies, attributed_day NOT NULL

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-30
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Postgres DDL is transactional — the whole revision runs in one
    # transaction, so the backfill (5) provably precedes SET NOT NULL (6).
    # Pitfall 3: UPDATE BEFORE ALTER, single revision.

    # 1. fx_rates (D-04) — keyed (rate_date, currency), no denormalized UAH.
    op.create_table(
        "fx_rates",
        sa.Column("rate_date", sa.Date, nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column(
            "fetched_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("rate_date", "currency"),
    )
    # 2. D-04 covering index for the DESC LATERAL lookup. A plain btree
    # suffices — Postgres scans it backward for the `ORDER BY rate_date DESC
    # LIMIT 1` fallback; no DESC ops needed.
    op.create_index(
        "ix_fx_rates_currency_rate_date",
        "fx_rates",
        ["currency", "rate_date"],
        postgresql_using="btree",
    )

    # 3. tracked_fx_currencies (D-05).
    op.create_table(
        "tracked_fx_currencies",
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "bootstrap_done",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("last_attempted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.PrimaryKeyConstraint("currency"),
    )
    # 4. Seed USD + EUR (D-05). No fx_rates rows seeded — lifespan bootstrap
    # fills them (D-04).
    op.execute(
        "INSERT INTO tracked_fx_currencies (currency, bootstrap_done) "
        "VALUES ('USD', false), ('EUR', false)"
    )

    # 5. Backfill attributed_day Kyiv-correctly (D-09) — BEFORE the NOT NULL.
    op.execute(
        "UPDATE transactions SET attributed_day = "
        "(time AT TIME ZONE 'Europe/Kyiv')::date "
        "WHERE attributed_day IS NULL"
    )
    # 6. Tighten to NOT NULL — AFTER the backfill (Pitfall 3 / D-09).
    op.alter_column("transactions", "attributed_day", nullable=False)


def downgrade() -> None:
    op.alter_column("transactions", "attributed_day", nullable=True)
    op.drop_table("tracked_fx_currencies")
    op.drop_index("ix_fx_rates_currency_rate_date", table_name="fx_rates")
    op.drop_table("fx_rates")
