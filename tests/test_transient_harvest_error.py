"""Tests for is_transient_harvest_error — the retry/terminal classifier for harvest.

Pure (no I/O). Pins which harvest failures leave the job inflight for re-harvest (a
dropped pooled DB connection, a transient storage/transport error) versus fail the job
(a bad result, an unexpected bug). The engine has already produced the OCR output by the
time we harvest, so infrastructure errors must not be recorded as the job's failure.
"""

import httpx
from sqlalchemy.exc import DBAPIError

from app.workers.poller import is_transient_harvest_error


def _dbapi_error(*, invalidated: bool) -> DBAPIError:
    err = DBAPIError("SELECT 1", None, Exception("connection was closed"))
    err.connection_invalidated = invalidated
    return err


def test_closed_connection_is_transient() -> None:
    # A pooled connection closed under us (e.g. asyncpg ConnectionDoesNotExistError) comes
    # back as a DBAPIError with connection_invalidated set — safe to re-harvest.
    assert is_transient_harvest_error(_dbapi_error(invalidated=True))


def test_db_error_without_invalidation_is_terminal() -> None:
    # A DB error that did not invalidate the connection is a real problem with the
    # statement/data, not a transport blip — fail fast.
    assert not is_transient_harvest_error(_dbapi_error(invalidated=False))


def test_transport_errors_are_transient() -> None:
    assert is_transient_harvest_error(httpx.ConnectError(""))
    assert is_transient_harvest_error(httpx.ReadTimeout("slow"))
    assert is_transient_harvest_error(httpx.PoolTimeout("no connection"))


def test_other_exceptions_are_terminal() -> None:
    assert not is_transient_harvest_error(KeyError("job_id"))
    assert not is_transient_harvest_error(ValueError("boom"))
