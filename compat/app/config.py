"""Settings for the compat shim: taas backend URL and Redis."""

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class EngineConfig(BaseModel):
    """A legacy engine id's domain, as read from taas's /api/v1/domains."""

    domain: str = ""
    label: str = ""
    description: str | None = None


class Settings(BaseSettings):
    taas_base_url: str = "http://api:8000"
    redis_url: str = "redis://redis:6379/0"
    compat_ttl_seconds: int = 3600

    # extra=ignore: stale env entries must not crash startup
    model_config = SettingsConfigDict(env_file=".env.compat", extra="ignore")
