"""Backfill window math.

Constants:
  MONO_STATEMENT_MAX_WINDOW_SECONDS — Mono cap (31d + 1h, Pitfall 5).
  MONO_STATEMENT_BACKFILL_WINDOW_DAYS — operating chunk size (1h+ headroom).

All Mono time math is in SECONDS, never milliseconds (Pitfall 5 sub-point;
Phase 1 invariant in `MonobankImporter.fetch_statement`).
"""

from collections.abc import Iterator
from datetime import datetime, timedelta

MONO_STATEMENT_MAX_WINDOW_SECONDS = 2_682_000  # 31d + 1h — Mono cap
MONO_STATEMENT_BACKFILL_WINDOW_DAYS = 30  # 1h+ headroom inside the cap


def backfill_chunks(now: datetime, months: int = 12) -> Iterator[tuple[datetime, datetime]]:
    """Yield (window_from, window_to) tuples in newest-first order.

    For months=12, yields 12 tuples covering [now - 360d, now] in 30d slices.
    DST-blind (UTC seconds at the API boundary; Mono accepts UNIX seconds).
    """
    for n in range(months):
        window_to = now - timedelta(days=n * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        window_from = now - timedelta(days=(n + 1) * MONO_STATEMENT_BACKFILL_WINDOW_DAYS)
        yield window_from, window_to
