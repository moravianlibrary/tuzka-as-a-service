"""The live_metrics rows of one snapshot (step 5, D6)."""

from datetime import datetime

from app.services.live_metrics import metric_rows

TS = datetime(2026, 10, 21, 8, 2, 19, 668317)


def test_rows_per_counter_and_backend() -> None:
    rows = metric_rows(
        TS,
        queued={"default|alto,txt": 3, "maps|geojson,json": 1},
        inflight={"default|alto,txt": 2},
        backends=[(1, "box1-gpu1", 2), (2, "2", 0)],
    )
    assert rows == [
        {
            "ts": "2026-10-21 08:02:19.668",
            "metric": "queued",
            "labels": {"domain": "default", "outputs": "alto,txt"},
            "value": 3,
        },
        {
            "ts": "2026-10-21 08:02:19.668",
            "metric": "queued",
            "labels": {"domain": "maps", "outputs": "geojson,json"},
            "value": 1,
        },
        {
            "ts": "2026-10-21 08:02:19.668",
            "metric": "inflight",
            "labels": {"domain": "default", "outputs": "alto,txt"},
            "value": 2,
        },
        {
            "ts": "2026-10-21 08:02:19.668",
            "metric": "backend_inflight",
            "labels": {"backend_id": "1", "backend": "box1-gpu1"},
            "value": 2,
        },
        {
            "ts": "2026-10-21 08:02:19.668",
            "metric": "backend_inflight",
            "labels": {"backend_id": "2", "backend": "2"},
            "value": 0,
        },
    ]


def test_an_idle_system_still_reports_its_backends() -> None:
    rows = metric_rows(TS, queued={}, inflight={}, backends=[(1, "cpu", 0)])
    assert [r["metric"] for r in rows] == ["backend_inflight"]


class _NoBackends:
    async def execute(self, stmt):
        class _Result:
            def all(self):
                return []

        return _Result()


async def test_snapshot_without_backends(redis_client) -> None:
    from app.services.live_metrics import snapshot
    from app.services.redis_jobs import LOAD_QUEUED_KEY

    await redis_client.hset(LOAD_QUEUED_KEY, "default|alto,txt", 4)
    rows = await snapshot(redis_client, _NoBackends())
    assert [(r["metric"], r["value"]) for r in rows] == [("queued", 4)]
