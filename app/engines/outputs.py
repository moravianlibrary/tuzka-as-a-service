"""Output-to-extension mapping and fmt ↔ output conversion."""

from __future__ import annotations

from app.vocabulary import Fmt

OUTPUT_EXT: dict[str, str] = {"alto": "xml", "txt": "txt"}


def ext_for(output: str) -> str:
    """Return the file extension for an output format; falls back to the name itself."""
    return OUTPUT_EXT.get(output, output)


def fmt_to_outputs(fmt: str) -> frozenset[str]:
    """Map a fmt string to its set of outputs."""
    if fmt == Fmt.MULTI:
        return frozenset({Fmt.ALTO.value, Fmt.TXT.value})
    if fmt in (Fmt.ALTO, Fmt.TXT):
        return frozenset({str(fmt)})
    raise ValueError(f"Unknown fmt: {fmt}")


def outputs_to_fmt(outputs: frozenset[str]) -> Fmt:
    """Map a set of outputs back to a fmt string.

    A single output returns itself; exactly alto+txt → multi.
    Anything else raises ``ValueError``.
    """
    if outputs == frozenset({Fmt.ALTO, Fmt.TXT}):
        return Fmt.MULTI
    if outputs == frozenset({Fmt.ALTO}):
        return Fmt.ALTO
    if outputs == frozenset({Fmt.TXT}):
        return Fmt.TXT
    raise ValueError(f"Unknown outputs: {outputs}")
