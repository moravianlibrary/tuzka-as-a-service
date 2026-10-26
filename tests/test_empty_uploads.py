"""Empty images are rejected before they become jobs or stored objects."""

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile

from app.exceptions import Unprocessable
from app.routers import batches, jobs


@pytest.mark.asyncio
async def test_empty_job_upload_creates_no_state(monkeypatch) -> None:
    monkeypatch.setattr(jobs.config_service, "get_domain_limits", AsyncMock(return_value={}))
    db = MagicMock()
    r = MagicMock()
    incoming = MagicMock()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(incoming_client=incoming)))
    settings = SimpleNamespace(allowed_extensions=[".jpg"], max_upload_bytes=100)

    with pytest.raises(Unprocessable, match="empty image") as exc_info:
        await jobs.submit_job(
            request=request,
            image=UploadFile(file=io.BytesIO(), filename="empty.jpg"),
            external_id=None,
            fmt="multi",
            domain=None,
            outputs=None,
            options=None,
            username="alice",
            principal=MagicMock(),
            db=db,
            r=r,
            settings=settings,
        )

    assert exc_info.value.status_code == 422
    db.add.assert_not_called()
    db.commit.assert_not_called()
    r.assert_not_called()
    incoming.assert_not_called()


@pytest.mark.asyncio
async def test_empty_tar_member_is_skipped(monkeypatch) -> None:
    image = (Path(__file__).resolve().parents[1] / "test-data" / "sample.jpg").read_bytes()
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, content in (("empty.jpg", b""), ("real.jpg", image)):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    data.seek(0)

    spec = SimpleNamespace(domain="default", outputs={"txt"}, options={})
    monkeypatch.setattr(batches, "resolve_job_spec", AsyncMock(return_value=spec))
    monkeypatch.setattr(batches, "check_scope", lambda *_: None)
    monkeypatch.setattr(batches.storage, "put_object", AsyncMock())
    monkeypatch.setattr(batches.config_service, "get_state_ttl_seconds", AsyncMock(return_value=60))
    monkeypatch.setattr(batches.redis_jobs, "init_batch", AsyncMock())
    enqueue = AsyncMock()
    monkeypatch.setattr(batches.redis_jobs, "enqueue_jobs", enqueue)
    monkeypatch.setattr(batches, "get_emitter", lambda: SimpleNamespace(emit=MagicMock()))
    db = MagicMock()
    db.commit = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    request = SimpleNamespace(
        headers={}, app=SimpleNamespace(state=SimpleNamespace(incoming_client=MagicMock()))
    )
    settings = SimpleNamespace(
        batch_max_upload_bytes=10_000_000,
        max_upload_bytes=10_000_000,
        batch_max_pages=10,
        allowed_extensions=[".jpg"],
        minio_incoming_bucket="incoming",
    )

    response = await batches.submit_batch(
        request=request,
        archive=UploadFile(file=data, filename="pages.tar"),
        external_id=None,
        domain=None,
        outputs="txt",
        options=None,
        username="alice",
        principal=MagicMock(),
        db=db,
        r=MagicMock(),
        settings=settings,
    )

    assert response.total == 1
    assert response.skipped == ["empty.jpg"]
    (queued,) = [call.args[1] for call in enqueue.await_args_list]
    assert len(queued) == 1  # one job, for the one non-empty image
    assert batches.storage.put_object.await_count == 1
