from datetime import datetime

from pydantic import BaseModel


class DashboardStats(BaseModel):
    total_jobs: int
    jobs_by_status: dict[str, int]
    jobs_today: int
    avg_duration_seconds: float | None


class DashboardUser(BaseModel):
    username: str
    total_jobs: int
    done: int
    failed: int
    last_active: datetime | None


class DashboardBackend(BaseModel):
    id: int
    url: str
    label: str | None
    enabled: bool
    max_inflight: int
    inflight_now: int
    healthy: bool | None = None
