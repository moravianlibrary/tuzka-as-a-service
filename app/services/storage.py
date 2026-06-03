from datetime import datetime, timedelta
from io import BytesIO

from miniopy_async import Minio

from app.config import Settings


def get_incoming_client(settings: Settings) -> Minio:
    url = settings.minio_incoming_url.replace("http://", "").replace("https://", "")
    secure = settings.minio_incoming_url.startswith("https://")
    return Minio(
        url,
        access_key=settings.minio_incoming_access_key,
        secret_key=settings.minio_incoming_secret_key,
        secure=secure,
    )


def get_results_client(settings: Settings) -> Minio:
    url = settings.minio_results_url.replace("http://", "").replace("https://", "")
    secure = settings.minio_results_url.startswith("https://")
    return Minio(
        url,
        access_key=settings.minio_results_access_key,
        secret_key=settings.minio_results_secret_key,
        secure=secure,
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
