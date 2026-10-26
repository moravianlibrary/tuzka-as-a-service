"""Pydantic schemas for user admin endpoints: creation, keys, and rate-limit overrides."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.runtime_config import RateLimit
from app.vocabulary import LimitClass

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


# A class's limit fields (the users columns) are rate_<name>_per_minute and burst_<name>.
LIMIT_FIELD_NAMES: dict[LimitClass, str] = {
    LimitClass.SUBMIT: "submit",
    LimitClass.QUERY: "query",
    LimitClass.WS_CONNECT: "ws",
}


def limit_fields(limit_class: LimitClass) -> tuple[str, str]:
    """The (per-minute, burst) field names of a limit class."""
    name = LIMIT_FIELD_NAMES[limit_class]
    return f"rate_{name}_per_minute", f"burst_{name}"


class UserLimitOverrides(BaseModel):
    """A user's own rate limits; None = the global default of that class."""

    rate_submit_per_minute: int | None = Field(default=None, ge=0, le=INT32_MAX)
    burst_submit: int | None = Field(default=None, ge=0, le=INT32_MAX)
    rate_query_per_minute: int | None = Field(default=None, ge=0, le=INT32_MAX)
    burst_query: int | None = Field(default=None, ge=0, le=INT32_MAX)
    rate_ws_per_minute: int | None = Field(default=None, ge=0, le=INT32_MAX)
    burst_ws: int | None = Field(default=None, ge=0, le=INT32_MAX)

    def apply(self, limit_class: LimitClass, default: RateLimit) -> RateLimit:
        """The class's default with this user's overrides of it applied."""
        per_minute, burst = (getattr(self, f) for f in limit_fields(limit_class))
        return default.overridden(per_minute, burst)


class UserUpdate(UserLimitOverrides):
    """PATCH body for a user: rate-limit overrides, priority, URL template, enable/disable."""

    active: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=INT32_MAX)

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


class _UserIdentity(BaseModel):
    username: str
    active: bool
    priority: int = 0
    created_at: datetime


class UserList(UserLimitOverrides, _UserIdentity):
    """A user as ``taas-admin user list`` shows it. Fields go base-last-first, so the
    identity columns lead, then the limit overrides, then the key's scopes."""

    # None = any domain / output.
    allowed_domains: list[str] | None = None
    allowed_outputs: list[str] | None = None


class EffectiveLimits(BaseModel):
    """The fields of UserLimitOverrides, resolved against the defaults, so never null."""

    rate_submit_per_minute: int
    burst_submit: int
    rate_query_per_minute: int
    burst_query: int
    rate_ws_per_minute: int
    burst_ws: int

    @classmethod
    def from_limits(cls, limits: Mapping[LimitClass, RateLimit]) -> "EffectiveLimits":
        fields: dict[str, int] = {}
        for limit_class, limit in limits.items():
            per_minute_field, burst_field = limit_fields(limit_class)
            fields[per_minute_field] = limit.per_minute
            fields[burst_field] = limit.burst
        return cls.model_validate(fields)


class UserLimitsResponse(BaseModel):
    username: str
    overrides: UserLimitOverrides
    effective: EffectiveLimits


class SetKeyRequest(BaseModel):
    key: str
