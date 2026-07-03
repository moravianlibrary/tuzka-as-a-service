"""Pydantic response schemas for the dashboard stats, user, and backend views."""

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


# Timestamps in these rows are pre-serialized ISO-8601 strings (the handlers isoformat
# the naive-UTC columns), so they are typed `str | None`, not `datetime`.
class DashboardJobRow(BaseModel):
    job_id: str
    username: str
    external_id: str
    status: str
    fmt: str
    domain: str | None
    submitted_at: str | None
    dispatched_at: str | None
    engine_received_at: str | None
    started_at: str | None
    finished_at: str | None
    stored_at: str | None
    backend_id: int | None
    backend: str | None
    engine_version: str | None
    error: str | None
    external_url: str | None


class DashboardJobsPage(BaseModel):
    jobs: list[DashboardJobRow]
    total: int


class UsageResponse(BaseModel):
    days: list[str]
    users: list[str]
    series: dict[str, list[int]]
    statuses: list[str]
    status_series: dict[str, list[int]]


class AnalyticsBreakdownRow(BaseModel):
    time_bucket: str | None
    username: str | None
    engine_version: str | None
    engine_device: str | None
    domain: str | None
    jobs_total: int
    jobs_done: int
    jobs_failed: int
    proc_avg_s: float | None
    proc_p95_s: float | None
    avg_alto_lines: float | None
    avg_alto_chars: float | None
    avg_mean_conf: float | None


class AnalyticsBreakdownPage(BaseModel):
    page: int
    has_next: bool
    rows: list[AnalyticsBreakdownRow]


class AnalyticsRawRow(BaseModel):
    job_id: str
    external_id: str | None
    submitted_at: str | None
    username: str | None
    engine_version: str | None
    engine_device: str | None
    domain: str | None
    fmt: str | None
    status: str | None
    file_size_bytes: int | None
    system_queue_s: float | None
    engine_queue_s: float | None
    ocr_running_s: float | None
    time_in_system_s: float | None
    alto_lines: int | None
    alto_blocks: int | None
    alto_chars: int | None
    mean_conf: float | None


class AnalyticsRawPage(BaseModel):
    page: int
    has_next: bool
    rows: list[AnalyticsRawRow]


class FacetsResponse(BaseModel):
    usernames: list[str]
    domains: list[str]
    engine_versions: list[str]
