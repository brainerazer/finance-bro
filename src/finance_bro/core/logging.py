import logging
import re
from typing import Any

import structlog

_REDACTED = "***REDACTED***"
_TOKEN_REGEX = re.compile(r"[A-Za-z0-9_-]{30,}")
_CONFIGURED = False


def _redact(
    _logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Mask token / X-Token / amount* keys at INFO+; replace token-shaped
    substrings in the event message. DEBUG bypasses redaction (Pattern 4)."""
    if method_name == "debug":
        return event_dict
    for k in list(event_dict.keys()):
        if re.search(r"token|amount", k, re.IGNORECASE):
            event_dict[k] = _REDACTED
    if isinstance(event_dict.get("event"), str):
        event_dict["event"] = _TOKEN_REGEX.sub(_REDACTED, event_dict["event"])
    return event_dict


def configure(level: str = "INFO") -> None:
    """Wire structlog with redaction processor. Idempotent — safe to call from
    both FastAPI lifespan and tests."""
    global _CONFIGURED
    # Quiet httpx access logs — never log Mono response body at INFO (Pitfall 4).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper())
        ),
        # Route through stdlib so a process-wide handler (and tests that
        # attach a StreamHandler to the root logger) can capture output.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    _CONFIGURED = True
