"""Pydantic response models for the Phase 1 API surface.

All money on the JSON boundary is integer minor units typed as Python `int`
(see TransactionOut below) — never str, never float, never Decimal — per
FX-01 / threat T6. Currency is the
ISO-4217 alpha string (length 3). The full Mono `statementItem` payload is
preserved verbatim as `raw_payload: dict` (D-10, ING-03).
"""

from datetime import datetime
from typing import Any

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


class ImportResultOut(BaseModel):
    polled_account_id: str
    statement_count: int
    inserted: int
    skipped_duplicates: int
