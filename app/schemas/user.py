"""Pydantic schemas for user admin endpoints: creation, keys, and rate-limit overrides."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Postgres INTEGER columns are 32-bit — bound numeric input so an out-of-range value is a
# clean 422 at the boundary instead of an asyncpg DataError (500) at write time.
INT32_MAX = 2_147_483_647


def _reject_nul(v: str | None) -> str | None:
    # Postgres text cannot store a NUL byte; reject it here rather than let asyncpg raise
    # CharacterNotInRepertoireError (500) mid-write.
    if v is not None and "\x00" in v:
        raise ValueError("must not contain NUL characters")
    return v


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=255)

    _no_nul = field_validator("username")(_reject_nul)


class UserResponse(BaseModel):
    username: str
    api_key: str


class UserLimitOverrides(BaseModel):
    rate_submit_per_minute: int | None = Field(default=None, ge=0, le=INT32_MAX)
    burst_submit: int | None = Field(default=None, ge=0, le=INT32_MAX)
    rate_query_per_minute: int | None = Field(default=None, ge=0, le=INT32_MAX)
    burst_query: int | None = Field(default=None, ge=0, le=INT32_MAX)
    rate_ws_per_minute: int | None = Field(default=None, ge=0, le=INT32_MAX)
    burst_ws: int | None = Field(default=None, ge=0, le=INT32_MAX)


class UserUpdate(UserLimitOverrides):
    """PATCH body for a user: rate-limit overrides, priority, URL template, enable/disable."""

    active: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=INT32_MAX)
    external_url_template: str | None = Field(default=None, max_length=2048)

    _no_nul = field_validator("external_url_template")(_reject_nul)

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        # active and priority back NOT NULL columns — an explicit null would hit a NotNull
        # IntegrityError (500). (Unset is absent; the rate-limit overrides stay nullable.)
        if isinstance(data, dict):
            nulled = [f for f in ("active", "priority") if f in data and data[f] is None]
            if nulled:
                raise ValueError(f"fields may not be null: {', '.join(nulled)}")
        return data


class UserList(UserLimitOverrides):
    username: str
    active: bool
    created_at: datetime
    priority: int = 0
    external_url_template: str | None = None


class EffectiveLimits(BaseModel):
    # Same fields as UserLimitOverrides but fully resolved, so never null.
    rate_submit_per_minute: int
    burst_submit: int
    rate_query_per_minute: int
    burst_query: int
    rate_ws_per_minute: int
    burst_ws: int


class UserLimitsResponse(BaseModel):
    username: str
    overrides: UserLimitOverrides
    effective: EffectiveLimits


class SetKeyRequest(BaseModel):
    key: str
