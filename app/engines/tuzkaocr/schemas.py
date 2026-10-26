"""TuzkaOCR's own HTTP responses (private to its adapter)."""

from pydantic import BaseModel

from app.engines.schemas import JobStatusBody, Metric


class ModelsBody(BaseModel):
    """``GET /api/v1/models``: the domains a request may select, and optionally the
    outputs each one produces."""

    selectable_via_domain: list[str | None] = []
    capabilities: dict[str, list[str]] | None = None


class TuzkaOcrStatus(JobStatusBody):
    n_lines: Metric = None
    mean_conf: Metric = None

    def metrics(self) -> dict[str, int | float | str]:
        return {
            key: value
            for key, value in (("n_lines", self.n_lines), ("mean_conf", self.mean_conf))
            if value is not None
        }
