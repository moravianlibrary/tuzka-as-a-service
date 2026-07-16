"""Boundary validation for the backend/user admin schemas.

These guard the fixes for the contract-test 500s: invalid admin input (empty/non-http URL,
NUL bytes, int32-overflowing numbers, explicit null on a NOT NULL column) must be rejected
at the Pydantic boundary as a 422 rather than reaching the DB and raising a 500.
"""

import pytest
from pydantic import ValidationError

from app.schemas.backend import INT32_MAX, BackendCreate, BackendUpdate
from app.schemas.user import UserCreate, UserUpdate


def test_backend_create_accepts_valid():
    b = BackendCreate(url="http://ocr-engine:8000", max_inflight=4)
    assert b.url == "http://ocr-engine:8000"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"url": ""},  # empty
        {"url": "ftp://x"},  # non-http scheme
        {"url": "http://x", "label": "a\x00b"},  # NUL byte -> CharacterNotInRepertoire
        {"url": "http://x", "max_inflight": INT32_MAX + 1},  # int32 overflow
        {"url": "http://x", "priority": INT32_MAX + 1},
    ],
)
def test_backend_create_rejects_invalid(kwargs):
    with pytest.raises(ValidationError):
        BackendCreate(**kwargs)


def test_backend_update_partial_is_allowed():
    # An unset field is simply absent — a label-only patch is valid.
    assert BackendUpdate(label="x").model_dump(exclude_unset=True) == {"label": "x"}


@pytest.mark.parametrize("field", ["url", "max_inflight", "enabled", "priority", "device", "managed"])
def test_backend_update_rejects_explicit_null_on_not_null_columns(field):
    with pytest.raises(ValidationError):
        BackendUpdate(**{field: None})


def test_user_create_rejects_nul_and_empty():
    UserCreate(username="alice")  # valid
    with pytest.raises(ValidationError):
        UserCreate(username="a\x00b")
    with pytest.raises(ValidationError):
        UserCreate(username="")


def test_user_update_bounds_and_nulls():
    # nullable overrides may be cleared with an explicit null
    assert UserUpdate(rate_submit_per_minute=None).model_dump(exclude_unset=True) == {
        "rate_submit_per_minute": None
    }
    with pytest.raises(ValidationError):
        UserUpdate(priority=INT32_MAX + 1)  # int32 overflow
    with pytest.raises(ValidationError):
        UserUpdate(active=None)  # NOT NULL column
    with pytest.raises(ValidationError):
        UserUpdate(priority=None)  # NOT NULL column
