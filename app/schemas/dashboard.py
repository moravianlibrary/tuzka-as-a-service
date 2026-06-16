from datetime import datetime

from pydantic import BaseModel


class DashboardStats(BaseModel):
    total_jobs: int
    jobs_by_status: dict[str, int]
    avg_ocr_running_seconds: float | None
    avg_time_in_system_seconds: float | None


class DashboardUser(BaseModel):
    username: str
    total_jobs: int
    done: int
    failed: int
    last_active: datetime | None
    # No referencing jobs -> the user can be hard-deleted (else only disabled).
    can_delete: bool = True


class DashboardBackend(BaseModel):
    id: int
    url: str
    label: str | None
    enabled: bool
    max_inflight: int
    inflight_now: int
    healthy: bool | None = None
    # No referencing jobs -> the backend can be hard-deleted (else only disabled).
    can_delete: bool = True
