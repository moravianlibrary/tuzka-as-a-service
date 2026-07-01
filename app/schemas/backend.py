from datetime import datetime

from pydantic import BaseModel, Field


class BackendCreate(BaseModel):
    url: str
    label: str | None = None
    api_key: str | None = None
    max_inflight: int = Field(default=4, ge=1)
    priority: int = Field(default=0, ge=0)
    device: str = "cpu"
    managed: bool = False


class BackendUpdate(BaseModel):
    url: str | None = None
    label: str | None = None
    api_key: str | None = None
    max_inflight: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0)
    device: str | None = None
    managed: bool | None = None


class BackendResponse(BaseModel):
    id: int
    url: str
    label: str | None
    enabled: bool
    max_inflight: int
    priority: int = 0
    device: str = "cpu"
    managed: bool = False
    created_at: datetime
