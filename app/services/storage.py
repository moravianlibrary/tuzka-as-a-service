from datetime import datetime, timedelta
from io import BytesIO

from miniopy_async import Minio

from app.config import Settings


def _make_client(url: str, access_key: str, secret_key: str, region: str) -> Minio:
    endpoint = url.replace("http://", "").replace("https://", "")
    return Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=url.startswith("https://"),
        # Pin the region so the SDK never issues a GetBucketLocation lookup against the
        # endpoint. That matters for the presign client, whose endpoint (the public URL)
        # is not reachable from this process — presigning must be a purely local operation.
        region=region,
    )


def get_incoming_client(settings: Settings) -> Minio:
    return _make_client(
        settings.minio_incoming_url,
        settings.minio_incoming_access_key,
        settings.minio_incoming_secret_key,
        settings.minio_region,
    )


def get_results_client(settings: Settings) -> Minio:
    """Client for results storage I/O (put/get/list/delete), reached over the internal network."""
    return _make_client(
        settings.minio_results_url,
        settings.minio_results_access_key,
        settings.minio_results_secret_key,
        settings.minio_region,
    )


def get_results_public_client(settings: Settings) -> Minio:
    """Client used only to presign result URLs.

    Presigned URLs are SigV4-signed against the client's endpoint host, so they must be
    signed for an address the *download client* can reach. ``minio_results_public_url``
    provides that externally-reachable endpoint; when unset we fall back to the internal
    URL (correct when clients share the network, e.g. local dev with localhost).
    """
    return _make_client(
        settings.minio_results_public_url or settings.minio_results_url,
        settings.minio_results_access_key,
        settings.minio_results_secret_key,
        settings.minio_region,
    )


async def put_object(client: Minio, bucket: str, path: str, data: bytes, content_type: str) -> None:
    stream = BytesIO(data)
    await client.put_object(bucket, path, stream, len(data), content_type=content_type)


async def get_object(client: Minio, bucket: str, path: str) -> bytes:
    response = await client.get_object(bucket, path)
    try:
        return await response.read()
    finally:
        response.close()
        await response.release()


async def presign_get(client: Minio, bucket: str, path: str, ttl_minutes: int) -> str:
    return await client.presigned_get_object(bucket, path, expires=timedelta(minutes=ttl_minutes))


async def delete_objects(client: Minio, bucket: str, paths: list[str]) -> None:
    from miniopy_async.deleteobjects import DeleteObject

    objects = [DeleteObject(p) for p in paths]
    errors = await client.remove_objects(bucket, objects)
    async for error in errors:
        print(f"Delete error: {error}")


async def list_expired_objects(client: Minio, bucket: str, older_than: datetime) -> list[str]:
    expired = []
    objects = client.list_objects(bucket, recursive=True)
    async for obj in objects:
        if obj.last_modified and obj.last_modified.replace(tzinfo=None) < older_than:
            expired.append(obj.object_name)
    return expired
