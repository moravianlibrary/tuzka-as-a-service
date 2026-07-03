"""Pydantic request/response schemas for the backend admin endpoints."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

# An engine runs on one of these devices; parsed at the boundary so the interior and
# admin handlers never re-check the string.
Device = Literal["cpu", "gpu"]


class BackendCreate(BaseModel):
    url: str
    label: str | None = None
    api_key: str | None = None
    max_inflight: int = Field(default=4, ge=1)
    priority: int = Field(default=0, ge=0)
    device: Device = "cpu"
    managed: bool = False


class BackendUpdate(BaseModel):
    url: str | None = None
    label: str | None = None
    api_key: str | None = None
    max_inflight: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0)
    device: Device | None = None
    managed: bool | None = None


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
