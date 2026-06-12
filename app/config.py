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
    # Externally-reachable endpoint used only to presign result URLs. SigV4 signs the
    # endpoint host, so it must be an address download clients can reach. Empty => use
    # minio_results_url (fine when clients share the network).
    minio_results_public_url: str = ""
    minio_results_access_key: str = "minioresults"
    minio_results_secret_key: str = "minioresults"
    minio_results_bucket: str = "results"
    # S3 region embedded in presigned-URL signatures. Pinned (not auto-discovered) so
    # presigning needs no GetBucketLocation network call; must match MINIO_SITE_REGION.
    minio_region: str = "eu-central-1"

    # Auth
    master_key: str = ""
    key_encryption_secret: str = ""

    # Upload constraints
    allowed_extensions: list[str] = [".tif", ".tiff", ".jpg", ".jpeg", ".png"]
    max_upload_bytes: int = 100 * 1024 * 1024  # 100 MB

    # Workers. Ticks tuned for sub-second-to-~1s OCR jobs: a 1s initial poll keeps
    # backend slots turning over near the job time. Backoff still adapts upward
    # (to poll_backoff_max) for slow/dense GPU jobs.
    submit_tick_seconds: float = 1.0
    poller_tick_seconds: float = 1.0
    poller_harvest_concurrency: int = 10
    poll_backoff_initial: float = 1.0
    poll_backoff_max: float = 30.0

    # Compression
    zstd_compression_level: int = 3

    # WebSocket
    ws_catch_up_seconds: int = 120

    # extra=ignore: stale env entries must not crash startup
    model_config = {"env_file": ".env.local", "extra": "ignore"}
