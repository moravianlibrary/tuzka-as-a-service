from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class EngineConfig(BaseModel):
    domain: str = ""
    label: str = ""
    description: str | None = None


class Settings(BaseSettings):
    taas_base_url: str = "http://api:8000"
    redis_url: str = "redis://redis:6379/0"
    compat_ttl_seconds: int = 3600

    engines: dict[int, EngineConfig] = {
        1: EngineConfig(domain="", label="Default", description="Default OCR engine"),
        2: EngineConfig(
            domain="kramarky",
            label="Kramarky",
            description="Engine for kramarky domain",
        ),
    }

    model_config = SettingsConfigDict(env_file=".env.compat")
