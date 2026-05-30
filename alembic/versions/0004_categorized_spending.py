"""categorized spending — categories, rules, transactions.category_id FK + seed

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-30

Adds the Phase 4 categorization schema:
  * `categories` — the editable default taxonomy (D-01/D-03).
  * `rules` — priority-ordered, first-match-wins predicate rows (D-04). MCC
    coverage ships as ORDINARY seeded rules, never a hardcoded MCC->category
    dict (D-04 / Pitfall 3).
  * FK `transactions.category_id -> categories.id` ON DELETE RESTRICT (D-03/D-15)
    on the pre-existing all-NULL column (Runtime State Inventory: safe).

Postgres DDL is transactional — the whole revision runs in one transaction, so
the category seed provably precedes the rule seed that subselects its ids.
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


# --- Default taxonomy (D-01; names/colors are Claude's discretion). ---
# (name, color) — Ukrainian-context groupings. Fully editable post-seed.
_CATEGORIES: list[tuple[str, str]] = [
    ("Groceries", "#22c55e"),
    ("Cafe & Restaurants", "#f97316"),
    ("Transport", "#3b82f6"),
    ("Fuel", "#eab308"),
    ("Utilities", "#06b6d4"),
    ("Health & Pharmacy", "#ef4444"),
    ("Shopping", "#a855f7"),
    ("Entertainment", "#ec4899"),
    ("Communications", "#14b8a6"),
    ("Cash & ATM", "#64748b"),
    ("Fees & Commissions", "#71717a"),
    ("Income", "#16a34a"),
    ("Transfers", "#94a3b8"),
    ("Travel", "#0ea5e9"),
    ("Other / Misc", "#9ca3af"),
]

# --- Default MCC seed rules (D-04; MCC ranges are Claude's discretion). ---
# Each is a debit + `mcc IN [...]` predicate -> a taxonomy category. These are
# ordinary editable/deletable rules; the engine is the SOLE categorization path.
# (priority, category_name, [mcc...], description)
_MCC_RULES: list[tuple[int, str, list[int], str]] = [
    (100, "Groceries", [5411, 5412, 5422, 5499], "MCC: grocery & supermarkets"),
    (200, "Cafe & Restaurants", [5811, 5812, 5813, 5814], "MCC: eating & drinking places"),
    (300, "Transport", [4111, 4121, 4131, 4789], "MCC: transit, taxi, bus"),
    (400, "Fuel", [5541, 5542], "MCC: service stations"),
    (500, "Utilities", [4900], "MCC: utilities"),
    (600, "Health & Pharmacy", [5912, 8011, 8021, 8062], "MCC: pharmacy, doctors, hospitals"),
    (700, "Shopping", [5311, 5651, 5732, 5999], "MCC: dept stores, apparel, electronics"),
    (800, "Entertainment", [7832, 7841, 5815], "MCC: cinema, streaming, digital goods"),
    (900, "Communications", [4814, 4812], "MCC: telecom"),
    (1000, "Cash & ATM", [6011], "MCC: ATM withdrawals"),
    (1100, "Travel", [3501, 3502, 3503, 7011], "MCC: lodging & airlines"),
]


def _mcc_predicate_json(mccs: list[int]) -> str:
    """A flat AND-only predicate: `mcc IN [...] AND amount_sign debit` (D-06)."""
    return json.dumps(
        {
            "all": [
                {"op": "in_int", "field": "mcc", "values": mccs},
                {"op": "amount_sign", "sign": "debit"},
            ]
        },
        separators=(",", ":"),
    )


def upgrade() -> None:
    # 1. categories (D-01/D-03) — mirror the TrackedFxCurrency seed-table shape.
    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("color", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("name", name="uq_categories_name"),
    )

    # 2. rules (D-04) — priority-ordered predicate rows. UNIQUE priority forbids
    # ties (Pitfall 6); `description` is a human label, NOT a predicate field
    # (D-07).
    op.create_table(
        "rules",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("priority", sa.Integer, nullable=False),
        sa.Column(
            "category_id",
            sa.BigInteger,
            sa.ForeignKey("categories.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("predicate", postgresql.JSONB, nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("priority", name="uq_rules_priority"),
    )
    op.create_index("ix_rules_priority", "rules", ["priority"])

    # 3. FK on the pre-existing all-NULL transactions.category_id (D-03/D-15).
    op.create_foreign_key(
        "fk_transactions_category",
        "transactions",
        "categories",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # 4. Seed taxonomy (D-01). Idiom: 0003's USD/EUR op.execute seed.
    categories_table = sa.table(
        "categories",
        sa.column("name", sa.Text),
        sa.column("color", sa.Text),
    )
    op.bulk_insert(
        categories_table,
        [{"name": name, "color": color} for name, color in _CATEGORIES],
    )

    # 5. Seed MCC default RULES (D-04). category_id resolved via subselect so the
    # seed is robust to category insert order. predicate is a JSON literal of the
    # closed-op AST shape (flat AND-only — D-06).
    for priority, category_name, mccs, description in _MCC_RULES:
        op.execute(
            sa.text(
                "INSERT INTO rules (priority, category_id, predicate, description) "
                "VALUES (:priority, "
                "(SELECT id FROM categories WHERE name = :category_name), "
                "CAST(:predicate AS JSONB), :description)"
            ).bindparams(
                priority=priority,
                category_name=category_name,
                predicate=_mcc_predicate_json(mccs),
                description=description,
            )
        )


def downgrade() -> None:
    # Reverse order — drop FK, index, rules, categories.
    op.drop_constraint("fk_transactions_category", "transactions", type_="foreignkey")
    op.drop_index("ix_rules_priority", table_name="rules")
    op.drop_table("rules")
    op.drop_table("categories")
