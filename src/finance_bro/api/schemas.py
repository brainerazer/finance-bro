"""Pydantic response models for the Phase 1 API surface.

All money on the JSON boundary is integer minor units typed as Python `int`
(see TransactionOut below) — never str, never float, never Decimal — per
FX-01 / threat T6. Currency is the
ISO-4217 alpha string (length 3). The full Mono `statementItem` payload is
preserved verbatim as `raw_payload: dict` (D-10, ING-03).

EXCEPTION (Phase 3, D-10): `TransactionOut.fx_rate` is transported as a
Decimal-as-string (e.g. "43.80330000"), never a float. It is the one deliberate
departure from "money is always int minor units" — an FX rate is not money, and
string transport preserves the exact NBU rate so the JS side never reintroduces
float drift (CLAUDE.md §Money).
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HealthOut(BaseModel):
    status: str
    db: str


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_kind: str
    source_account_id: str
    currency: str = Field(min_length=3, max_length=3)
    # Mono card flavor (black/platinum/white/eAid/...). Nullable for non-card
    # source_kinds (jars, FOPs); the column is populated by 02-01's migration
    # backfill + 02-03's CanonicalAccount.mono_type wiring. Surfaced here so
    # 02-04's status surface can render per-card breakdowns.
    mono_type: str | None = None


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    source_tx_id: str
    amount_minor: int
    currency: str = Field(min_length=3, max_length=3)
    time: datetime
    # ING-05 (Plan 02-02): `hold:true` rows are pending Mono authorizations.
    # Always populated — Transaction.hold is non-null with server_default 'false'.
    hold: bool
    raw_payload: dict[str, Any]
    # Phase 3 (D-10/D-11/D-12) — UAH rollup computed ON READ via the LATERAL join
    # (FX-03; never a denormalized column). For native-UAH rows uah_amount_minor
    # == amount_minor and fx_rate == "1.00000000". When no NBU rate is available
    # the value fields are null and fx_stale is True (the row still appears).
    uah_amount_minor: int | None = None
    fx_rate: str | None = None  # Decimal-as-string ("43.80330000"); never float
    fx_rate_date: date | None = None
    fx_source: Literal["native_uah", "mono_card", "nbu"]
    fx_stale: bool


class ImportResultOut(BaseModel):
    polled_account_id: str
    statement_count: int
    inserted: int
    skipped_duplicates: int


# ----- Phase 2 (D-14) — Status surface -----


class SchedulerStatusOut(BaseModel):
    state: str = Field(description="One of: running, auth_failed, stopped")
    since: datetime
    last_error: str | None = None


class AccountStatusOut(BaseModel):
    account_id: int
    source_account_id: str
    mono_type: str | None = None
    last_polled_at: datetime | None = None
    last_poll_inserted: int | None = None
    # v1: always 0; DB stores inserted+updated combined in import_runs.inserted
    # (deferred v1.5 split — D-14). TODO: add a separate `updated_in_place` column
    # to import_runs in v1.5 to surface insert/update breakdown distinctly.
    last_poll_updated: int = 0
    last_poll_statement_count: int | None = None
    last_status: str | None = None
    last_error: str | None = None
    backfill_remaining: int = 0
    backfill_total: int = 0


class BackfillStatusOut(BaseModel):
    state: str = Field(description="One of: idle, running")
    runs_remaining: int
    runs_total: int
    eta_seconds: int | None = None


class ImportStatusOut(BaseModel):
    scheduler: SchedulerStatusOut
    accounts: list[AccountStatusOut]
    backfill: BackfillStatusOut


# ----- Phase 2 (D-16) — Force-poll enqueue -----


class ImportEnqueueRowOut(BaseModel):
    account_id: int
    run_id: int


class ImportEnqueuedOut(BaseModel):
    enqueued: list[ImportEnqueueRowOut]


# ----- Phase 2 (D-07) — Backfill enqueue -----


class BackfillEnqueueIn(BaseModel):
    account_id: int | None = None
    months: int = Field(default=12, ge=1, le=36)


class BackfillEnqueueOut(BaseModel):
    run_ids: list[int]
