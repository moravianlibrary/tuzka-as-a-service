"""Tests for is_transient_harvest_error — the retry/terminal classifier for harvest.

Pure (no I/O). Pins which harvest failures keep the job on the harvest queue for a retry (a
dropped pooled DB connection, a transient storage/transport error) versus fail the job
(a bad result, an unexpected bug). The engine has already produced the OCR output by the
time we harvest, so infrastructure errors must not be recorded as the job's failure.
"""

import httpx
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.engines.base import OutputUnavailable
from app.workers.poller.steps import is_transient_harvest_error


def _dbapi_error() -> DBAPIError:
    return DBAPIError("SELECT 1", None, Exception("syntax error"))


def _closed_connection_error() -> DBAPIError:
    return DBAPIError(
        "SELECT 1", None, Exception("connection was closed"), connection_invalidated=True
    )


def test_closed_connection_is_transient() -> None:
    # A pooled connection closed under us (e.g. asyncpg ConnectionDoesNotExistError) comes
    # back as a DBAPIError with connection_invalidated set — safe to re-harvest.
    assert is_transient_harvest_error(_closed_connection_error())


def test_db_error_without_invalidation_is_terminal() -> None:
    # A DB error that did not invalidate the connection is a real problem with the
    # statement/data, not a transport blip — fail fast.
    assert not is_transient_harvest_error(_dbapi_error())


def test_transport_errors_are_transient() -> None:
    assert is_transient_harvest_error(httpx.ConnectError(""))
    assert is_transient_harvest_error(httpx.ReadTimeout("slow"))
    assert is_transient_harvest_error(httpx.PoolTimeout("no connection"))


def test_a_connection_that_cannot_be_opened_is_transient() -> None:
    # The database (or storage) is down: the backend lookup or the store can't connect.
    assert is_transient_harvest_error(ConnectionRefusedError("refused"))
    assert is_transient_harvest_error(OSError("name does not resolve"))
    assert is_transient_harvest_error(PoolTimeout("pool exhausted"))


def test_other_exceptions_are_terminal() -> None:
    assert not is_transient_harvest_error(KeyError("job_id"))
    assert not is_transient_harvest_error(LookupError("no engine_job_id"))
    assert not is_transient_harvest_error(ValueError("boom"))


def test_unavailable_output_is_terminal() -> None:
    # The engine finished but can't produce an output (TuzkaM: georeferencing failed);
    # re-harvesting asks the same question, so the job fails instead of waiting.
    assert not is_transient_harvest_error(OutputUnavailable("gcp", "georeferencing failed"))
