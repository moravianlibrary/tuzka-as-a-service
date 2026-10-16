"""Pure retry classification and backoff helpers.

Every worker (dispatch, poll, engine) calls these instead of keeping
its own rules.  No I/O — fully unit-testable.
"""

from __future__ import annotations

import logging
import random
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

Stage = Literal["dispatch", "poll", "engine", "harvest"]

# --- Constants (not config; see D6) ---
_BACKOFF_BASE = 1.0  # seconds
_BACKOFF_CAP = 30.0  # seconds


def describe_exception(exc: Exception) -> str:
    """A never-empty, human-readable description of an exception.

    Some transport errors (notably httpx connect/read errors) carry no message, so
    ``str(exc)`` is empty — which previously stored a blank ``error`` and left the
    dashboard's error box (shown only for a truthy error) hidden. Fall back to
    ``repr`` so the exception type always survives to the DB and the UI.
    """
    return str(exc) or repr(exc)


def http_status_of(exc: Exception) -> int | None:
    """Return the HTTP status code from an ``HTTPStatusError``, else ``None``."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def is_retryable_dispatch_error(exc: Exception) -> bool:
    """True if a dispatch failure looks transient — worth retrying on another backend.

    Pure (no I/O) so it's unit-testable. Transport-level failures (connect/read/write/pool
    errors and timeouts) and 5xx engine responses point at an unhealthy or overloaded
    backend, not a bad request, so the same job may succeed elsewhere. Everything else —
    4xx (bad image, auth, unsupported domain), malformed responses, unexpected bugs — is
    terminal: retrying would just loop until the requeue budget is spent. (Engine 503 is
    handled earlier as EngineFullError, so it never reaches here.)

    Per D1: also treats 408 (request timeout) and 429 (rate limit) as retryable.
    """
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code >= 500 or code in (408, 429)
    return False


def is_retryable_poll_error(exc: Exception) -> bool:
    """Same as dispatch, plus HTTP 404 (engine restarted, lost the job) — D1."""
    if is_retryable_dispatch_error(exc):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 404
    return False


def is_retryable_engine_failure(error_status: int | None) -> bool:
    """True if an engine-reported failure is worth retrying.

    Per D3 (stopgap for old engines): ``None`` means we couldn't determine the status,
    so we treat it as retryable.  Per D1: 5xx is retryable.  Per D2: 4xx is not.
    """
    if error_status is None:
        return True
    return error_status >= 500


def backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    """Full-jitter backoff.

    ``delay = random.uniform(0, min(cap, base * 2**attempt))``

    Per D6: base 1 s, cap 30 s.  *attempt* is the 0-based requeue count.
    """
    r = rng or random.Random()
    delay = min(_BACKOFF_CAP, _BACKOFF_BASE * (2**attempt))
    return r.uniform(0, delay)
