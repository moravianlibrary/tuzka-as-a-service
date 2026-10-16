"""Output-to-extension mapping and fmt ↔ output conversion."""

from __future__ import annotations

OUTPUT_EXT: dict[str, str] = {"alto": "xml", "txt": "txt"}


def ext_for(output: str) -> str:
    """Return the file extension for an output format; falls back to the name itself."""
    return OUTPUT_EXT.get(output, output)


def fmt_to_outputs(fmt: str) -> frozenset[str]:
    """Map a fmt string to its set of outputs."""
    if fmt == "multi":
        return frozenset({"alto", "txt"})
    if fmt in ("alto", "txt"):
        return frozenset({fmt})
    raise ValueError(f"Unknown fmt: {fmt}")


def outputs_to_fmt(outputs: frozenset[str]) -> str:
    """Map a set of outputs back to a fmt string.

    A single output returns itself; exactly alto+txt → multi.
    Anything else raises ``ValueError``.
    """
    if outputs == frozenset({"alto", "txt"}):
        return "multi"
    if outputs == frozenset({"alto"}):
        return "alto"
    if outputs == frozenset({"txt"}):
        return "txt"
    raise ValueError(f"Unknown outputs: {outputs}")
