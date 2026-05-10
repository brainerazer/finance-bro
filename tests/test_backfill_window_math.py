"""Pure-function unit tests for `finance_bro.scheduler.window`.

PATTERNS.md Archetype A: no DB, no HTTP, no fixtures. Verifies the constants
match Pitfall 5 (Mono cap = 31d + 1h) and that `backfill_chunks` produces
12 newest-first 30d windows ready for ImportRunRepo.enqueue_backfill.
"""

from datetime import UTC, datetime, timedelta

from finance_bro.scheduler.window import (
    MONO_STATEMENT_BACKFILL_WINDOW_DAYS,
    MONO_STATEMENT_MAX_WINDOW_SECONDS,
    backfill_chunks,
)


def test_constants_match_pitfall_5() -> None:
    assert MONO_STATEMENT_MAX_WINDOW_SECONDS == 2_682_000
    assert MONO_STATEMENT_BACKFILL_WINDOW_DAYS == 30


def test_twelve_chunks_newest_first() -> None:
    """ING-06 + D-09 + Pitfall 5: 12 chunks of 30d, newest-first."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    chunks = list(backfill_chunks(now, months=12))
    assert len(chunks) == 12
    # Newest-first: chunk[0].window_to is `now`; subsequent windows go backwards.
    assert chunks[0][1] == now
    assert chunks[0][0] == now - timedelta(days=30)
    # Each subsequent chunk is 30d older.
    for n in range(1, 12):
        prev_from = chunks[n - 1][0]
        assert chunks[n][1] == prev_from
        assert chunks[n][0] == prev_from - timedelta(days=30)
    # Deepest chunk: 360 days back.
    assert chunks[11][0] == now - timedelta(days=360)


def test_each_chunk_within_mono_max_window() -> None:
    """Defensive: every chunk's seconds-span is below the Mono cap with headroom."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    for window_from, window_to in backfill_chunks(now, months=12):
        span_seconds = (window_to - window_from).total_seconds()
        assert span_seconds <= MONO_STATEMENT_MAX_WINDOW_SECONDS
        # 30d = 2_592_000s; cap = 2_682_000s; headroom = 90_000s = 25h.


def test_no_milliseconds_in_unix_conversion() -> None:
    """Pitfall 5 sub-point: never multiply by 1000. backfill_chunks yields datetimes;
    int(dt.timestamp()) gives SECONDS, which is what Mono expects."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    window_from, window_to = next(backfill_chunks(now, months=1))
    # Sanity: a 30d window in seconds is 2_592_000 (not 2_592_000_000).
    assert int(window_to.timestamp()) - int(window_from.timestamp()) == 30 * 86_400


def test_zero_months_returns_empty() -> None:
    """Edge: months=0 yields no chunks (defensive; not directly used but well-defined)."""
    now = datetime(2026, 5, 10, tzinfo=UTC)
    assert list(backfill_chunks(now, months=0)) == []
