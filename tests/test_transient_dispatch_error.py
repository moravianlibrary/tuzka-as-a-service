"""Tests for is_transient_dispatch_error — the retry/terminal classifier for dispatch.

Pure (no I/O). Pins which dispatch failures are retried on another backend (transport
errors, 5xx) versus failed fast (4xx, malformed responses, unexpected bugs).
"""

import httpx

from app.workers.submit import is_transient_dispatch_error


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine/api/v1/process")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


def test_transport_errors_are_transient() -> None:
    assert is_transient_dispatch_error(httpx.ConnectError(""))
    assert is_transient_dispatch_error(httpx.ReadTimeout("slow"))
    assert is_transient_dispatch_error(httpx.PoolTimeout("no connection"))


def test_5xx_is_transient() -> None:
    assert is_transient_dispatch_error(_status_error(500))
    assert is_transient_dispatch_error(_status_error(502))
    assert is_transient_dispatch_error(_status_error(504))


def test_4xx_is_terminal() -> None:
    assert not is_transient_dispatch_error(_status_error(400))
    assert not is_transient_dispatch_error(_status_error(401))
    assert not is_transient_dispatch_error(_status_error(422))


def test_other_exceptions_are_terminal() -> None:
    # A malformed engine response (missing job_id) or any unexpected bug fails fast —
    # retrying can't fix a bad request.
    assert not is_transient_dispatch_error(KeyError("job_id"))
    assert not is_transient_dispatch_error(ValueError("boom"))
