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
    priority: int = 0
    device: str = "cpu"
    # True for backends owned by the Helm deploy (upserted on each deploy); shown as a
    # badge so operators know manual edits are transient.
    managed: bool = False
    healthy: bool | None = None
    # Domains this backend currently serves (from GET /api/v1/models on each healthcheck).
    domains: list[str] = []
    # No referencing jobs -> the backend can be hard-deleted (else only disabled).
    can_delete: bool = True
