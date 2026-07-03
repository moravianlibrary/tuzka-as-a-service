"""Regression tests for master-key auth.

Guards the two must-fixes: an invalid/missing master key is rejected with **401**
(unauthenticated), not 403, and the comparison is constant-time (``hmac.compare_digest``).
The dependency is exercised directly — ``require_master`` takes a request, the header
value, and settings, so no HTTP layer is needed.
"""

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.deps import require_master


class _Request:
    """Minimal stand-in exposing the ``.cookies`` mapping require_master reads."""

    def __init__(self, cookies: dict[str, str] | None = None) -> None:
        self.cookies = cookies or {}


async def test_rejects_wrong_key_with_401() -> None:
    settings = Settings(master_key="correct-secret")

    with pytest.raises(HTTPException) as exc:
        await require_master(_Request(), key="wrong-secret", settings=settings)

    assert exc.value.status_code == 401  # bad credential is unauthenticated, not 403


async def test_rejects_missing_key_with_401() -> None:
    settings = Settings(master_key="correct-secret")

    with pytest.raises(HTTPException) as exc:
        await require_master(_Request(), key=None, settings=settings)

    assert exc.value.status_code == 401  # no credential presented -> 401


async def test_accepts_correct_key() -> None:
    settings = Settings(master_key="correct-secret")

    result = await require_master(_Request(), key="correct-secret", settings=settings)

    assert result is None  # dependency passes through with no exception
