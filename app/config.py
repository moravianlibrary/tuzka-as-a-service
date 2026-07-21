"""Typed application settings loaded from the environment."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Postgres
    database_url: str = "postgresql+asyncpg://taas:taas@localhost:5432/taas"
    # Pooled connections can be silently closed by the server (idle timeout, restart)
    # or a proxy (pgbouncer) between checkouts; asyncpg only notices mid-statement and
    # raises ConnectionDoesNotExistError. pre_ping validates each connection on checkout
    # and transparently replaces dead ones; recycle retires connections before a typical
    # server-side idle timeout can close them.
    db_pool_pre_ping: bool = True
    db_pool_recycle_seconds: int = 1800

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
    # Both workers are event-driven (they block on a wakeup signal); these are the
    # max idle wait between passes when no signal arrives, not fixed cadences. The
    # poller additionally wakes at each job's scheduled next_poll_at (bounded by this).
    submit_tick_seconds: float = 1.0
    poller_tick_seconds: float = 1.0
    poller_harvest_concurrency: int = 10
    poll_backoff_initial: float = 1.0
    poll_backoff_max: float = 5.0
    # Submit-worker caches: how often to reload the backend list, re-check a backend's
    # health, and re-sync a backend's served domains.
    backend_refresh_seconds: float = 30.0
    health_cache_seconds: float = 10.0
    domain_sync_seconds: float = 300.0

    # Compression
    zstd_compression_level: int = 3

    # WebSocket
    ws_catch_up_seconds: int = 120

    # Logging
    log_level: str = "INFO"

    # extra=ignore: stale env entries must not crash startup
    model_config = {"env_file": ".env.local", "extra": "ignore"}
