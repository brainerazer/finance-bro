"""Transaction repository — single owner of writes/reads against `transactions`.

`insert_many` uses `INSERT ... ON CONFLICT (account_id, source_tx_id) WHERE NOT
is_deleted DO UPDATE SET hold = EXCLUDED.hold, amount_minor = EXCLUDED.amount_minor,
raw_payload = EXCLUDED.raw_payload` against the partial unique index
`uq_transactions_account_source_tx` (migration 0001). On conflict EXACTLY THREE
columns mutate (D-10): every other column — including manual-edit columns from
Phases 4-6 (is_user_locked / category_id / category_source / description / mcc /
attributed_day) — is FROZEN BY OMISSION. The `(xmax = 0)` returning trick
distinguishes inserts from updates so the runner can log both counts.
"""

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import literal_column, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from finance_bro.categorizer.fields import RowView
from finance_bro.db.models import Transaction
from finance_bro.importers.base import CanonicalTransaction
from finance_bro.importers.currency_map import numeric_to_alpha
from finance_bro.services import fx_rollup

_KYIV = ZoneInfo("Europe/Kyiv")

# D-14 (verbatim, locked — RESEARCH Pattern 1 Approach A `text()`): the read path
# computes the UAH rollup ON READ via a LEFT JOIN LATERAL that carries the most
# recent NBU rate forward (`rate_date <= attributed_day ORDER BY rate_date DESC
# LIMIT 1`). LEFT (not INNER) so a transaction with no rate still appears (D-12).
# The rollup is never denormalized onto `transactions` (FX-03).
ROLLUP_SQL = text(
    """
    SELECT t.id, t.account_id, t.source_tx_id, t.amount_minor, t.currency,
           t.time, t.hold, t.raw_payload, t.attributed_day,
           t.category_id, t.category_source,
           c.name AS category_name, c.color AS category_color,
           fx.rate AS fx_rate, fx.rate_date AS fx_rate_date
    FROM transactions t
    LEFT JOIN categories c ON c.id = t.category_id
    LEFT JOIN LATERAL (
        SELECT rate, rate_date FROM fx_rates
        WHERE currency = t.currency AND rate_date <= t.attributed_day
        ORDER BY rate_date DESC LIMIT 1
    ) fx ON true
    WHERE t.account_id = :account_id AND NOT t.is_deleted
    ORDER BY t.time DESC
    """
)


def _op_currency_alpha(raw_payload: dict[str, Any]) -> str | None:
    """Best-effort op-currency lookup for the fx_source label (D-11). A missing
    or unmapped numeric `currencyCode` falls back to None (-> fx_source "nbu")
    rather than raising — the rollup must never blow up on a read."""
    code = raw_payload.get("currencyCode")
    if code is None:
        return None
    try:
        return numeric_to_alpha(int(code))
    except (ValueError, TypeError):
        return None


class TransactionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_many(
        self,
        account_id: int,
        items: list[CanonicalTransaction],
    ) -> tuple[int, int]:
        """Upsert canonical transactions idempotently.

        On conflict (i.e., a row with the same (account_id, source_tx_id) WHERE NOT
        is_deleted already exists), the upsert mutates EXACTLY THREE columns:
        `hold`, `amount_minor`, `raw_payload` (D-10). All other columns — currency,
        time, account_id, source_tx_id, created_at, is_user_locked, category_id,
        category_source, is_deleted, description, mcc, attributed_day — are FROZEN
        BY OMISSION. Phase 1's Pitfall-10 promise that the importer never overwrites
        manual edits stays a hard invariant.

        Returns `(inserted, updated_in_place)`. The `xmax = 0` trick: PostgreSQL's
        `xmax` system column is 0 on freshly-inserted rows; ON CONFLICT DO UPDATE
        sets it to the current transaction id. RESEARCH.md Pattern 3 + Pitfall 6.
        """
        if not items:
            return (0, 0)
        rows = [
            {
                "account_id": account_id,
                "source_tx_id": t.source_tx_id,
                "amount_minor": t.amount_minor,
                "currency": t.currency,
                "time": t.occurred_at,
                "raw_payload": t.raw,
                # On first INSERT, the importer is allowed to populate description/mcc
                # (Discretion bullet 8 + PATTERNS.md transformation §2). They become
                # immutable after the row exists because they are absent from the
                # on-conflict update clause below — D-10 frozen-by-omission.
                "description": t.description,
                "mcc": t.mcc,
                "hold": t.hold,
                # D-09: attributed_day is NOT NULL and frozen on first write. The
                # importer supplies the Kyiv calendar day; when absent, derive it
                # from occurred_at (UTC) → Kyiv date here as a safety net. Absent
                # from set_={...} below, so an upsert never moves it.
                "attributed_day": (
                    t.attributed_day
                    if t.attributed_day is not None
                    else t.occurred_at.astimezone(_KYIV).date()
                ),
            }
            for t in items
        ]
        stmt = insert(Transaction).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "source_tx_id"],
            index_where=text("NOT is_deleted"),
            set_={
                "hold": stmt.excluded.hold,
                "amount_minor": stmt.excluded.amount_minor,
                "raw_payload": stmt.excluded.raw_payload,
            },
        ).returning(
            Transaction.id,
            literal_column("(xmax = 0)").label("inserted"),
        )
        result = await self._s.execute(stmt)
        rows_back = result.all()
        inserted = sum(1 for r in rows_back if r.inserted)
        updated = len(rows_back) - inserted
        return (inserted, updated)

    # D-09 / Pitfall 1: locked rows are filtered IN SQL (`NOT is_user_locked`),
    # so they never reach the engine — defense-in-depth alongside the engine's
    # own SKIP. `NOT is_deleted` keeps soft-deleted rows out. Bound params only
    # (`= ANY(:ids)`); never f-string SQL (T-4-sqli).
    _FETCH_FOR_CATEGORIZE_SQL = text(
        """
        SELECT id, account_id, amount_minor, currency, hold, mcc, description,
               category_id, is_user_locked, raw_payload
        FROM transactions
        WHERE account_id = :account_id
          AND source_tx_id = ANY(:ids)
          AND NOT is_user_locked
          AND NOT is_deleted
        """
    )

    async def fetch_for_categorize(
        self,
        account_id: int,
        touched_source_tx_ids: list[str],
    ) -> list[RowView]:
        """Load the non-locked, non-deleted touched rows as pure `RowView`s for
        the engine (D-11). Locked rows are excluded in SQL (D-09 / Pitfall 1).
        Empty input short-circuits (no `ANY('{}')` round-trip)."""
        if not touched_source_tx_ids:
            return []
        result = await self._s.execute(
            self._FETCH_FOR_CATEGORIZE_SQL,
            {"account_id": account_id, "ids": touched_source_tx_ids},
        )
        rows: list[RowView] = []
        for m in result.mappings().all():
            # raw_payload may be NULL in theory; mirror `_op_currency_alpha`'s
            # never-raise discipline by defaulting to an empty dict.
            raw: dict[str, Any] = m["raw_payload"] or {}
            rows.append(
                RowView(
                    id=m["id"],
                    amount_minor=m["amount_minor"],
                    hold=m["hold"],
                    is_user_locked=m["is_user_locked"],
                    category_id=m["category_id"],
                    description=m["description"],
                    mcc=m["mcc"],
                    account_id=m["account_id"],
                    currency=m["currency"],
                    raw_payload=raw,
                )
            )
        return rows

    # D-14 / Pitfall 1: the account-wide counterpart to fetch_for_categorize — the
    # history sweep reads the WHOLE account's non-locked, non-deleted rows (drops
    # the `source_tx_id = ANY(...)` touched filter; KEEPS `NOT is_user_locked` so a
    # locked row never reaches the engine). Same column set + RowView build as the
    # touched read; bound params only (T-4-sqli).
    _FETCH_ALL_FOR_CATEGORIZE_SQL = text(
        """
        SELECT id, account_id, amount_minor, currency, hold, mcc, description,
               category_id, is_user_locked, raw_payload
        FROM transactions
        WHERE account_id = :account_id
          AND NOT is_user_locked
          AND NOT is_deleted
        """
    )

    # CR-01 (TOCTOU fix): the FOR UPDATE variant used by the history-sweep COMMIT
    # path. Reading the to-be-written rows under a row lock inside the same
    # transaction that recomputes the staleness token AND writes back closes the
    # read→write gap: a concurrent import tick (or a future manual lock-setter)
    # that touches one of these rows must serialize behind this transaction, and
    # any state change it made before we locked flips the recomputed token →
    # StaleRunError (409). Identical projection to the read-only variant above so
    # RowView construction is shared; FOR UPDATE only legal inside a transaction.
    _FETCH_ALL_FOR_CATEGORIZE_FOR_UPDATE_SQL = text(
        """
        SELECT id, account_id, amount_minor, currency, hold, mcc, description,
               category_id, is_user_locked, raw_payload
        FROM transactions
        WHERE account_id = :account_id
          AND NOT is_user_locked
          AND NOT is_deleted
        FOR UPDATE
        """
    )

    # Count of locked (non-deleted) rows in the account — the history sweep's
    # `skipped_locked_count` (D-12). Locked rows are excluded from the sweep by
    # _FETCH_ALL_FOR_CATEGORIZE_SQL; this counts how many were left untouched.
    _COUNT_LOCKED_SQL = text(
        """
        SELECT count(*) FROM transactions
        WHERE account_id = :account_id
          AND is_user_locked
          AND NOT is_deleted
        """
    )

    async def fetch_all_for_categorize(
        self, account_id: int, *, for_update: bool = False
    ) -> list[RowView]:
        """Load EVERY non-locked, non-deleted row in the account as pure
        `RowView`s for the history sweep (D-14). The account-wide counterpart to
        `fetch_for_categorize`: locked rows are excluded in SQL (D-09 / Pitfall 1).
        Reuses the identical RowView construction + raw_payload `.get()` safety.

        `for_update=True` (CR-01) acquires a row lock on every returned row and is
        ONLY valid inside an open transaction — the history-sweep commit path uses
        it so the recompute→token-check→write sequence is atomic. The read-only
        preview path leaves it False."""
        sql = (
            self._FETCH_ALL_FOR_CATEGORIZE_FOR_UPDATE_SQL
            if for_update
            else self._FETCH_ALL_FOR_CATEGORIZE_SQL
        )
        result = await self._s.execute(sql, {"account_id": account_id})
        rows: list[RowView] = []
        for m in result.mappings().all():
            raw: dict[str, Any] = m["raw_payload"] or {}
            rows.append(
                RowView(
                    id=m["id"],
                    amount_minor=m["amount_minor"],
                    hold=m["hold"],
                    is_user_locked=m["is_user_locked"],
                    category_id=m["category_id"],
                    description=m["description"],
                    mcc=m["mcc"],
                    account_id=m["account_id"],
                    currency=m["currency"],
                    raw_payload=raw,
                )
            )
        return rows

    async def count_locked(self, account_id: int) -> int:
        """How many locked (non-deleted) rows the history sweep skips (D-12)."""
        result = await self._s.execute(self._COUNT_LOCKED_SQL, {"account_id": account_id})
        return int(result.scalar_one())

    # Targeted, parameterized write-back. Locked rows are excluded upstream by
    # fetch_for_categorize / fetch_all_for_categorize and by the engine's SKIP;
    # the `AND NOT is_user_locked` WHERE guard (WR-02 / CAT-04) is defense-in-depth
    # so this write can NEVER clobber a manually-locked row even if a stale (id)
    # reached it. A (id, None) update writes category_id NULL with
    # category_source='rule' (D-02: the row was evaluated, matched nothing — it
    # is stamped 'rule', never silently bucketed into a real category).
    _APPLY_CATEGORY_SQL = text(
        "UPDATE transactions SET category_id = :cid, category_source = 'rule' "
        "WHERE id = :tid AND NOT is_user_locked"
    )

    async def apply_categories(self, updates: list[tuple[int, int | None]]) -> None:
        """Write `category_id` (+ `category_source='rule'`) for exactly the rows
        the engine returned. Small batch is fine for single-user volumes; each
        UPDATE is parameterized (T-4-sqli). Rows not in `updates` are untouched."""
        for tid, cid in updates:
            await self._s.execute(self._APPLY_CATEGORY_SQL, {"cid": cid, "tid": tid})

    async def list_for_account(self, account_id: int) -> list[dict[str, Any]]:
        """Read path: every row joined with its carry-forward NBU rate via the
        D-14 LATERAL query, then enriched with the computed UAH rollup (FX-03/
        FX-04). Returns plain dict rows carrying the transaction columns PLUS the
        five computed FX fields (`uah_amount_minor`, `fx_rate`, `fx_rate_date`,
        `fx_source`, `fx_stale`). The rollup math is delegated to
        `fx_rollup.rollup` so the no-double-conversion property (FX-04) lives in
        one place; the repo only owns the SQL + the per-row composition.
        """
        result = await self._s.execute(ROLLUP_SQL, {"account_id": account_id})
        out: list[dict[str, Any]] = []
        for m in result.mappings().all():
            row = dict(m)
            fx = fx_rollup.rollup(
                amount_minor=row["amount_minor"],
                currency=row["currency"],
                account_currency=row["currency"],
                fx_rate=row["fx_rate"],
                fx_rate_date=row["fx_rate_date"],
                attributed_day=row["attributed_day"],
                op_currency_alpha=_op_currency_alpha(row["raw_payload"]),
            )
            row["uah_amount_minor"] = fx.uah_amount_minor
            row["fx_rate"] = fx.fx_rate
            row["fx_rate_date"] = fx.fx_rate_date
            row["fx_source"] = fx.fx_source
            row["fx_stale"] = fx.fx_stale
            out.append(row)
        return out
