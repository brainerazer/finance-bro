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


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    account_id: int
    source_tx_id: str
    amount_minor: int
    currency: str = Field(min_length=3, max_length=3)
    time: datetime
    raw_payload: dict[str, Any]


class ImportResultOut(BaseModel):
    polled_account_id: str
    statement_count: int
    inserted: int
    skipped_duplicates: int
