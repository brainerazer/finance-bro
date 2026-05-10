"""phase 2 sync — accounts.mono_type, scheduler_state singleton, import_runs

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-10
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # 1. accounts.mono_type — backfill from raw_payload->>'type' (Pitfall 7).
    op.add_column("accounts", sa.Column("mono_type", sa.Text, nullable=True))
    op.execute(
        "UPDATE accounts "
        "SET mono_type = raw_payload->>'type' "
        "WHERE source_kind = 'mono.card'"
    )

    # 2. scheduler_state singleton (D-15 + RESEARCH.md Pattern 5).
    op.create_table(
        "scheduler_state",
        sa.Column("id", sa.Integer, nullable=False),
        sa.Column(
            "state", sa.Text, nullable=False, server_default=sa.text("'running'")
        ),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "since",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("id = 1", name="ck_scheduler_state_singleton"),
        sa.CheckConstraint(
            "state IN ('running','stopped','auth_failed')",
            name="ck_scheduler_state_state",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO scheduler_state (id, state) VALUES (1, 'running')")

    # 3. import_runs (D-08 shape).
    op.create_table(
        "import_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "account_id",
            sa.BigInteger,
            sa.ForeignKey("accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("run_kind", sa.Text, nullable=False),
        sa.Column("window_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("window_to", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "status", sa.Text, nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column(
            "attempts", sa.Integer, nullable=False, server_default=sa.text("0")
        ),
        sa.Column("statement_count", sa.Integer, nullable=True),
        sa.Column("inserted", sa.Integer, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "run_kind IN ('backfill','live')", name="ck_import_runs_run_kind"
        ),
        sa.CheckConstraint(
            "status IN ('pending','in_flight','done','error')",
            name="ck_import_runs_status",
        ),
    )
    # Pitfall 5 — index for the status-page DISTINCT ON join.
    op.create_index(
        "ix_import_runs_account_kind_completed",
        "import_runs",
        ["account_id", "run_kind"],
        postgresql_using="btree",
    )
    # claim_next_pending: WHERE status='pending' ORDER BY created_at ASC.
    op.create_index(
        "ix_import_runs_status_created",
        "import_runs",
        ["status", "created_at"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("ix_import_runs_status_created", table_name="import_runs")
    op.drop_index(
        "ix_import_runs_account_kind_completed", table_name="import_runs"
    )
    op.drop_table("import_runs")
    op.drop_table("scheduler_state")
    op.drop_column("accounts", "mono_type")
