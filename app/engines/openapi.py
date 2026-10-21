"""Helpers shared by adapters of FastAPI engines: the per-job options schema derived from
an engine's OpenAPI document, and how option values go into its multipart form."""

from __future__ import annotations

from typing import Any

# Form fields of POST /api/v1/process that taas sets itself; the rest are options.
_RESERVED_FIELDS = frozenset({"image", "domain", "fmt"})


def options_schema_from_openapi(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Derive the per-job options JSON Schema from the engine's OpenAPI document.

    Takes the multipart form body of ``POST /api/v1/process``, drops the fields taas
    owns (image, domain, fmt) and closes the object, so an unknown option is rejected
    at submit instead of being silently ignored by the engine. Returns None when the
    document doesn't describe that body.
    """
    try:
        body = spec["paths"]["/api/v1/process"]["post"]["requestBody"]
        schema: dict[str, Any] = body["content"]["multipart/form-data"]["schema"]
        ref = schema.get("$ref")
        if ref is not None:
            # FastAPI emits a local ref: #/components/schemas/Body_process_image_...
            name = ref.removeprefix("#/components/schemas/")
            schema = spec["components"]["schemas"][name]
        properties: dict[str, Any] = schema.get("properties", {})
    except (KeyError, TypeError, AttributeError):
        return None

    options: dict[str, Any] = {
        "type": "object",
        "properties": {k: v for k, v in properties.items() if k not in _RESERVED_FIELDS},
        "additionalProperties": False,
    }
    required = [r for r in schema.get("required", []) if r not in _RESERVED_FIELDS]
    if required:
        options["required"] = required
    return options


def form_value(value: Any) -> str:
    """Render an option as a form field; booleans the way FastAPI parses them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
