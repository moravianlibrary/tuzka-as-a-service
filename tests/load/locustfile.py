"""Locust load test for the taas public API.

Models a client's submit -> poll-status loop plus light read traffic. Auth uses an API key
from ``TAAS_API_KEY`` (``make load-test`` seeds one and sets it). Run headless, e.g.:

    TAAS_API_KEY=... uv run --extra dev locust -f tests/load/locustfile.py \
        --host http://localhost:8080 --headless -u 20 -r 5 -t 30s

Scope: with no workers/OCR engine running this exercises ingest + status/read paths, not
end-to-end OCR throughput. Point it at a full stack (`make up`) to load the dispatch
pipeline too. Rate-limit responses (429) are counted as expected backpressure, not failures.
"""

import os
import uuid

from locust import HttpUser, between, task

API_KEY = os.environ.get("TAAS_API_KEY", "")
# Submit stores the upload bytes without decoding them, so any small blob named .jpg passes
# the extension/size checks — no real image file needed to load the ingest path.
_IMAGE = b"\xff\xd8\xff\xe0load-test-payload"


class TaasUser(HttpUser):
    wait_time = between(0.5, 2.0)

    def on_start(self) -> None:
        self.client.headers.update({"X-API-Key": API_KEY})
        self.job_ids: list[str] = []

    @task(5)
    def submit(self) -> None:
        with self.client.post(
            "/api/v1/jobs",
            files={"image": ("load.jpg", _IMAGE, "image/jpeg")},
            data={"uuid": str(uuid.uuid4()), "fmt": "multi"},
            name="POST /api/v1/jobs",
            catch_response=True,
        ) as r:
            if r.status_code == 202:
                job_id = r.json().get("job_id")
                if job_id:
                    self.job_ids.append(job_id)
                    del self.job_ids[:-50]  # keep the last 50
                r.success()
            elif r.status_code == 429:
                r.success()  # rate-limited under load is expected backpressure
            else:
                r.failure(f"unexpected {r.status_code}")

    @task(3)
    def poll_status(self) -> None:
        if not self.job_ids:
            return
        with self.client.get(
            f"/api/v1/jobs/{self.job_ids[-1]}",
            name="GET /api/v1/jobs/{id}",
            catch_response=True,
        ) as r:
            if r.status_code in (200, 404, 429):
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")

    @task(1)
    def list_jobs(self) -> None:
        with self.client.get(
            "/api/v1/jobs?limit=20", name="GET /api/v1/jobs", catch_response=True
        ) as r:
            if r.status_code in (200, 429):
                r.success()
            else:
                r.failure(f"unexpected {r.status_code}")
