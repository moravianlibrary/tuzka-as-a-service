from datetime import datetime

from pydantic import BaseModel


class UserCreate(BaseModel):
    username: str


class UserResponse(BaseModel):
    username: str
    api_key: str


class UserLimitOverrides(BaseModel):
    rate_submit_per_minute: int | None = None
    burst_submit: int | None = None
    rate_query_per_minute: int | None = None
    burst_query: int | None = None
    rate_ws_per_minute: int | None = None
    burst_ws: int | None = None


class UserList(UserLimitOverrides):
    username: str
    active: bool
    created_at: datetime


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
