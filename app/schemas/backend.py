from datetime import datetime

from pydantic import BaseModel


class BackendCreate(BaseModel):
    url: str
    label: str | None = None
    api_key: str | None = None
    max_inflight: int = 4
    priority: int = 0
    device: str = "cpu"
    managed: bool = False


class BackendUpdate(BaseModel):
    url: str | None = None
    label: str | None = None
    api_key: str | None = None
    max_inflight: int | None = None
    enabled: bool | None = None
    priority: int | None = None
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
