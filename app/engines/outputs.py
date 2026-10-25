"""Output-to-extension mapping."""

from __future__ import annotations

OUTPUT_EXT: dict[str, str] = {"alto": "xml", "txt": "txt"}


def ext_for(output: str) -> str:
    """Return the file extension for an output format; falls back to the name itself."""
    return OUTPUT_EXT.get(output, output)
