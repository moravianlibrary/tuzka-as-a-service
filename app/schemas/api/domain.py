"""Pydantic schemas for the domain list response (GET /api/v1/domains)."""

from typing import Any

from pydantic import BaseModel


class DomainEntry(BaseModel):
    name: str
    label: str | None
    description: str | None
    # Union of the outputs the enabled backends produce for this domain.
    outputs: list[str]
    # JSON Schema for the submit `options` field; None = no options accepted yet.
    options_schema: dict[str, Any] | None
    # The engine id the legacy compat API exposes for this domain.
    legacy_engine_id: int | None


class DomainListResponse(BaseModel):
    domains: list[DomainEntry]
