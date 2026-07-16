"""Tests for describe_exception — the never-empty exception describer.

Pure (no I/O), so runs without redis/DB. Pins the one behavior that matters: a
message-less exception (e.g. an httpx transport error) still yields a non-empty
string, so the stored ``error`` and the dashboard are never blank.
"""

import httpx

from app.workers.submit import describe_exception


def test_message_bearing_exception_uses_str() -> None:
    assert describe_exception(ValueError("boom")) == "boom"


def test_empty_message_falls_back_to_repr() -> None:
    # An exception raised with no args has an empty str() — the exact case that stored
    # a blank error for the failed dispatch we debugged.
    result = describe_exception(httpx.ConnectError(""))
    assert result != ""
    assert "ConnectError" in result


def test_bare_exception_never_empty() -> None:
    assert describe_exception(RuntimeError()) != ""
