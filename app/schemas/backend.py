"""Pydantic request/response schemas for the backend admin endpoints."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# An engine runs on one of these devices; parsed at the boundary so the interior and
# admin handlers never re-check the string.
Device = Literal["cpu", "gpu"]

# Postgres INTEGER columns are 32-bit — bound numeric input so an out-of-range value is a
# clean 422 at the boundary instead of an asyncpg DataError (500) at insert time.
INT32_MAX = 2_147_483_647

# Columns whose model definition is NOT NULL: reject an explicit `null` on update (an unset
# field is simply absent, so this only fires when the caller literally sends `"field": null`,
# which would otherwise hit a NotNull IntegrityError -> 500).
_NON_NULLABLE = ("url", "max_inflight", "enabled", "priority", "device", "managed")


def _reject_nul(v: str | None) -> str | None:
    # Postgres text cannot store a NUL byte; reject it here rather than let asyncpg raise
    # CharacterNotInRepertoireError (500) mid-insert.
    if v is not None and "\x00" in v:
        raise ValueError("must not contain NUL characters")
    return v


def _validate_url(v: str | None) -> str | None:
    if v is None:
        return v
    if not (v.startswith("http://") or v.startswith("https://")):
        raise ValueError("url must start with http:// or https://")
    return v


class BackendCreate(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    label: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=1024)
    max_inflight: int = Field(default=4, ge=1, le=INT32_MAX)
    priority: int = Field(default=0, ge=0, le=INT32_MAX)
    device: Device = "cpu"
    managed: bool = False

    _check_url = field_validator("url")(_validate_url)
    _no_nul = field_validator("url", "label", "api_key")(_reject_nul)


class BackendUpdate(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    label: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=1024)
    max_inflight: int | None = Field(default=None, ge=1, le=INT32_MAX)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=INT32_MAX)
    device: Device | None = None
    managed: bool | None = None

    _check_url = field_validator("url")(_validate_url)
    _no_nul = field_validator("url", "label", "api_key")(_reject_nul)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        if isinstance(data, dict):
            nulled = [f for f in _NON_NULLABLE if f in data and data[f] is None]
            if nulled:
                raise ValueError(f"fields may not be null: {', '.join(nulled)}")
        return data


class BackendResponse(BaseModel):
    id: int
    url: str
    label: str | None
    enabled: bool
    max_inflight: int
    priority: int = 0
    # Reflects the stored value as-is (input is validated as `Device` on create/update).
    device: str = "cpu"
    managed: bool = False
    created_at: datetime
