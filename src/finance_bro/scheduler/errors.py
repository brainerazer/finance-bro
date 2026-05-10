"""Typed Mono errors at the importer boundary.

The runner branches on these per D-15 (401 sticky, 429 transient, transient
otherwise). Importer methods raise these instead of leaking raw
`httpx.HTTPStatusError` so the scheduler never has to read HTTP status
strings to decide intent.
"""


class MonoAuthError(Exception):
    """Raised when Mono returns 401. Sticky — sets scheduler_state='auth_failed' (D-15)."""


class MonoRateLimitError(Exception):
    """Raised when Mono returns 429. Transient — surfaced per-call only (D-15)."""

    def __init__(self, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Mono 429 (Retry-After={retry_after_seconds})")


class MonoTransientError(Exception):
    """Raised on 5xx / connect-timeout / other 4xx. Per-call import_runs.error;
    the next tick tries the next pending row."""
