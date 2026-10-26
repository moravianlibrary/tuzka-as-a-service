"""The values taas reads from the ``config`` table, parsed once where they're read
(docs/typing.md). Every key is optional there: a missing or malformed value falls back
to the built-in default, so an admin typo degrades a setting instead of 500ing."""

import logging
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.vocabulary import LimitClass

log = logging.getLogger(__name__)

# storage.<bucket>_ttl_minutes: how long the cleanup keeps a bucket's objects.
_STORAGE_TTL_KEY = re.compile(r"storage\.(?P<bucket>.+)_ttl_minutes")
STORAGE_TTL_DEFAULT_MINUTES = 60
RATE_LIMIT_PREFIX = "rate_limit."


class InvalidConfigEntry(ValueError):
    """A config value taas wouldn't read: an unknown key, or a malformed value."""


class RateLimit(BaseModel):
    """A GCRA limit: ``per_minute`` sustained, ``burst`` extra at once. Not bounded here:
    the limiter itself treats a non-positive rate as "deny" rather than fail."""

    model_config = ConfigDict(frozen=True)

    per_minute: int
    burst: int

    @classmethod
    def from_config(cls, value: Any, fallback: "RateLimit") -> "RateLimit":
        """A ``rate_limit.<class>`` row; a key it leaves out keeps the fallback's value."""
        if not isinstance(value, dict):
            return fallback
        try:
            return cls.model_validate(fallback.model_dump() | value)
        except ValidationError:
            return fallback

    def overridden(self, per_minute: int | None, burst: int | None) -> "RateLimit":
        """This limit with a user's overrides applied; None keeps this limit's value."""
        return RateLimit(
            per_minute=self.per_minute if per_minute is None else per_minute,
            burst=self.burst if burst is None else burst,
        )


# Used while no rate_limit.<class> row exists (migration 002 seeds the same values).
FALLBACK_RATE_LIMITS: dict[LimitClass, RateLimit] = {
    LimitClass.SUBMIT: RateLimit(per_minute=60, burst=10),
    LimitClass.QUERY: RateLimit(per_minute=120, burst=20),
    LimitClass.WS_CONNECT: RateLimit(per_minute=5, burst=2),
}


class RuntimeConfig(BaseModel):
    """Every runtime policy value of the ``config`` table, each field under its key.

    Built from all rows at once by ``from_rows``; a malformed value is logged and left
    at its default, so it degrades one setting rather than every reader of the table.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    queued_timeout_seconds: int = Field(900, alias="jobs.queued_timeout_seconds")
    running_timeout_seconds: int = Field(300, alias="jobs.running_timeout_seconds")
    max_requeues: int = Field(5, alias="jobs.max_requeues")
    # Retention: finished job rows, and result data.
    done_ttl_seconds: int = Field(86_400, alias="jobs.done_ttl_seconds")
    failed_ttl_seconds: int = Field(604_800, alias="jobs.failed_ttl_seconds")
    presigned_ttl_minutes: int = Field(60, alias="presigned.ttl_minutes")
    results_max_age_seconds: int = Field(86_400, alias="storage.results_max_age_seconds")
    # The results storage cap; None (unset) = no cap, results go by age only.
    results_max_bytes: int | None = Field(None, alias="storage.results_max_bytes")
    # Dispatch: a backend already running a batch's pages wins its next page when it
    # would finish within this fraction of the fastest one.
    affinity_tolerance: float = Field(0.15, alias="dispatch.affinity_tolerance")
    # Seconds per page /load assumes for a domain with no measured pages yet.
    default_seconds_per_page: float = Field(10.0, alias="load.default_seconds_per_page")
    # Per bucket, from storage.<bucket>_ttl_minutes; see storage_ttl_minutes.
    bucket_ttl_minutes: dict[str, int] = Field(default_factory=dict)
    rate_limits: dict[LimitClass, RateLimit] = Field(default=FALLBACK_RATE_LIMITS)

    @classmethod
    def from_rows(cls, rows: Mapping[str, Any]) -> "RuntimeConfig":
        """Parse the table's ``key -> value`` rows; keys taas doesn't read are ignored."""
        fields = {key: value for key, value in rows.items() if value is not None}
        fields["bucket_ttl_minutes"] = {
            match["bucket"]: value
            for key, value in fields.items()
            if (match := _STORAGE_TTL_KEY.fullmatch(key))
        }
        fields["rate_limits"] = {
            limit_class: RateLimit.from_config(
                rows.get(f"{RATE_LIMIT_PREFIX}{limit_class}"), fallback
            )
            for limit_class, fallback in FALLBACK_RATE_LIMITS.items()
        }
        try:
            return cls.model_validate(fields)
        except ValidationError as e:
            malformed = {str(err["loc"][0]) for err in e.errors()}
            log.warning("config: malformed %s; using the defaults", ", ".join(sorted(malformed)))
            return cls.model_validate({k: v for k, v in fields.items() if k not in malformed})

    def storage_ttl_minutes(self, bucket: str) -> int:
        return self.bucket_ttl_minutes.get(bucket, STORAGE_TTL_DEFAULT_MINUTES)

    @classmethod
    def parse_entry(cls, key: str, value: Any) -> Any:
        """``value`` as it should be stored under ``key``. Reading falls back to the
        default on a malformed value; writing refuses it instead, so an admin's typo is
        reported rather than silently ignored. Raises ``InvalidConfigEntry``."""
        if key.startswith(RATE_LIMIT_PREFIX):
            return _parse_rate_limit_entry(key, value)

        # Strict: the CLI hands over parsed JSON, so "5" or true for a number is a typo.
        adapter: TypeAdapter[Any] = TypeAdapter(
            cls._annotation_for(key), config=ConfigDict(strict=True)
        )
        try:
            return adapter.dump_python(adapter.validate_python(value), mode="json")
        except ValidationError as e:
            raise InvalidConfigEntry(f"{key}: {_first_error(e)}") from e

    @classmethod
    def _annotation_for(cls, key: str) -> Any:
        if _STORAGE_TTL_KEY.fullmatch(key):
            return int
        for field in cls.model_fields.values():
            if field.alias == key:
                return field.annotation
        raise InvalidConfigEntry(f"unknown config key {key!r}")


def _parse_rate_limit_entry(key: str, value: Any) -> dict[str, int]:
    """A ``rate_limit.<class>`` value: ``per_minute`` and/or ``burst``; a key it leaves
    out keeps the built-in value (``RateLimit.from_config``)."""
    limit_class = key.removeprefix(RATE_LIMIT_PREFIX)
    if limit_class not in LimitClass:
        raise InvalidConfigEntry(f"unknown config key {key!r}")
    if not isinstance(value, dict):
        raise InvalidConfigEntry(f"{key}: expected an object with per_minute and/or burst")
    unknown = sorted(set(value) - set(RateLimit.model_fields))
    if unknown:
        raise InvalidConfigEntry(f"{key}: unknown field(s) {', '.join(unknown)}")

    fallback = FALLBACK_RATE_LIMITS[LimitClass(limit_class)]
    try:
        merged = RateLimit.model_validate(fallback.model_dump() | value, strict=True)
    except ValidationError as e:
        raise InvalidConfigEntry(f"{key}: {_first_error(e)}") from e
    return merged.model_dump(include=set(value))


def _first_error(error: ValidationError) -> str:
    detail = error.errors()[0]
    where = ".".join(str(part) for part in detail["loc"])
    return f"{where}: {detail['msg']}" if where else detail["msg"]
