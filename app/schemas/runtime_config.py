"""The values taas reads from the ``config`` table, parsed once where they're read
(docs/typing.md). Every key is optional there: a missing or malformed value falls back
to the built-in default, so an admin typo degrades a setting instead of 500ing."""

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from app.vocabulary import LimitClass


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
