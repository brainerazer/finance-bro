import io
import logging

import structlog

from finance_bro.core.logging import configure

REAL_TOKEN = "AbCdEfGhIj1234567890_qrstuvwxyzABCDEFGHIJ"  # 41 chars, matches token regex


def _capture(level: str = "INFO"):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    configure(level=level)
    return buf


def test_no_token_in_logs():
    buf = _capture("INFO")
    log = structlog.get_logger()
    log.info("imported", token=REAL_TOKEN)
    out = buf.getvalue()
    assert "***REDACTED***" in out
    assert REAL_TOKEN not in out


def test_no_amounts_in_logs():
    buf = _capture("INFO")
    log = structlog.get_logger()
    log.info("paid", amount_minor=8500, amount=99)
    out = buf.getvalue()
    assert "8500" not in out
    assert "99" not in out
    assert "***REDACTED***" in out


def test_no_x_token_header():
    buf = _capture("INFO")
    log = structlog.get_logger()
    log.info("call", **{"X-Token": REAL_TOKEN})
    out = buf.getvalue()
    assert REAL_TOKEN not in out


def test_token_substring_in_event_message():
    buf = _capture("INFO")
    log = structlog.get_logger()
    log.info(f"calling api with token={REAL_TOKEN}")
    out = buf.getvalue()
    assert REAL_TOKEN not in out
    assert "***REDACTED***" in out


def test_debug_bypasses_redaction():
    buf = _capture("DEBUG")
    log = structlog.get_logger()
    log.debug("trace", token=REAL_TOKEN)
    out = buf.getvalue()
    # At DEBUG, raw passes through — this is the explicit Pattern 4 contract.
    assert REAL_TOKEN in out
