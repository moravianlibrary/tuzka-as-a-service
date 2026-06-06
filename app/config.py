from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Postgres
    database_url: str = "postgresql+asyncpg://taas:taas@localhost:5432/taas"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # MinIO incoming
    minio_incoming_url: str = "http://localhost:9000"
    minio_incoming_access_key: str = "minioincoming"
    minio_incoming_secret_key: str = "minioincoming"
    minio_incoming_bucket: str = "incoming"

    # MinIO results
    minio_results_url: str = "http://localhost:9010"
    minio_results_access_key: str = "minioresults"
    minio_results_secret_key: str = "minioresults"
    minio_results_bucket: str = "results"

    # Auth
    master_key: str = ""
    key_encryption_secret: str = ""

    # Upload constraints
    allowed_extensions: list[str] = [".tif", ".tiff", ".jpg", ".jpeg", ".png"]
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB

    # Workers
    submit_tick_seconds: float = 2.0
    poller_tick_seconds: float = 2.0
    poller_harvest_concurrency: int = 10
    job_ttl_seconds: int = 3600
    poll_backoff_initial: float = 2.0
    poll_backoff_max: float = 30.0

    # Compression
    zstd_compression_level: int = 3

    # WebSocket
    ws_catch_up_seconds: int = 120

    # Presigned URLs
    presigned_ttl_minutes: int = 60

    # extra=ignore: stale env entries must not crash startup
    model_config = {"env_file": ".env.local", "extra": "ignore"}
