"""Batch rules (step 7), pure: which archive members become pages, and their order."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class UploadedPage:
    """A batch page already in the incoming bucket, waiting to be queued."""

    job_id: UUID
    ext: str  # the member's extension, lowercased, with the dot
    megapixels: float

    def incoming_path(self, username: str) -> str:
        return f"{username}/{self.job_id}{self.ext}"


def plan_batch_member(name: str, allowed_extensions: list[str]) -> Literal["take", "skip"]:
    """A member becomes a page if it has an allowed image extension; anything else is
    skipped and listed in the response, without failing the batch (D3)."""
    return "take" if PurePosixPath(name).suffix.lower() in allowed_extensions else "skip"


# Queue scores are unix seconds; pages of one batch sit this far apart, in archive order.
PAGE_SCORE_STEP = 1e-6


def page_score(batch_submitted_at: float, index: int) -> float:
    """The queue score of a batch's ``index``-th page: the batch's submit time plus a tiny
    step, so one batch drains in order and doesn't interleave with a single job submitted
    a second later (D9)."""
    return batch_submitted_at + index * PAGE_SCORE_STEP
