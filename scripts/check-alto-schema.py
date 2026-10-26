#!/usr/bin/env python3
"""Validate the ALTO that TuzkaOCR emits against the real ALTO 4.4 schema.

`build_alto()` hand-builds its XML and *declares* ALTO 4.4 in xsi:schemaLocation; this
script is what checks that the claim holds. Two kinds of input:

  * generated — `build_alto()` run over synthetic pages that reach the branches a real
    page rarely does (role tags, non-ASCII IDs, coordinate clipping, empty pages).
    Skipped when ./TuzkaOCR is absent, since the engine is cloned separately.
  * real — ALTO files on disk, `test-data/out/*.alto.xml` by default (whatever the last
    `make e2e` left behind), or the paths given as arguments.

The schema is vendored under schemas/alto/ and loaded offline; see that README for why.

Usage:
    python scripts/check-alto-schema.py [FILE ...] [--no-generate] [--quiet]
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import types
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import xmlschema

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas" / "alto"
ALTO_XSD = SCHEMA_DIR / "alto-4-4.xsd"
XLINK_XSD = SCHEMA_DIR / "xlink.xsd"
XLINK_NS = "http://www.w3.org/1999/xlink"
ENGINE_ALTO_PY = ROOT / "TuzkaOCR" / "tuzkaocr" / "alto.py"
DEFAULT_GLOB = "test-data/out/*.alto.xml"
MAX_ERRORS_SHOWN = 10

CYAN, GREEN, RED, DIM, OFF = "\033[1;36m", "\033[1;32m", "\033[1;31m", "\033[2m", "\033[0m"


def load_schema() -> xmlschema.XMLSchema:
    """Build the ALTO 4.4 schema with no network access.

    alto-4-4.xsd imports the xlink namespace from a www.loc.gov URL that is both remote
    and Cloudflare-guarded, so the import is remapped to the vendored copy. The location
    must be an absolute URI — a relative one resolves against the ALTO file's own
    directory. `use_fallback=False` keeps xmlschema from quietly substituting its
    packaged xlink schema, which declares `simpleAttrs` rather than the `simpleLink`
    group ALTO references, and promoting import warnings to errors means a schema that
    failed to resolve can never masquerade as one that validated.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("error", xmlschema.XMLSchemaImportWarning)
        return xmlschema.XMLSchema(
            str(ALTO_XSD),
            locations=[(XLINK_NS, XLINK_XSD.as_uri())],
            allow="local",
            use_fallback=False,
        )


def load_engine_alto() -> types.ModuleType:
    """Import the engine's alto.py by path, bypassing the tuzkaocr package __init__.

    Importing `tuzkaocr` pulls in the inference pipeline (onnxruntime et al.), which the
    taas dev env has no reason to install. alto.py itself is stdlib-only, but it uses
    relative imports (`._version`), so register a bare package stub whose __path__ points
    at the engine package without running its __init__.
    """
    pkg = types.ModuleType("tuzkaocr")
    pkg.__path__ = [str(ENGINE_ALTO_PY.parent)]
    sys.modules.setdefault("tuzkaocr", pkg)
    spec = importlib.util.spec_from_file_location("tuzkaocr.alto", ENGINE_ALTO_PY)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {ENGINE_ALTO_PY}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _line(hpos: int, vpos: int, words: list[str], role: str | None = None) -> dict:
    """One text line, words laid out left to right at a fixed pitch."""
    line = {
        "hpos": hpos,
        "vpos": vpos,
        "width": 60 * len(words),
        "height": 40,
        "words": [(w, hpos + 60 * i, vpos, 55, 40) for i, w in enumerate(words)],
    }
    if role is not None:
        line["role"] = role
    return line


def generated_cases(build_alto: Callable[..., str]) -> Iterator[tuple[str, str]]:
    """Yield (case name, ALTO document) pairs covering the generator's branches."""
    yield (
        "minimal single block",
        build_alto("page-1", 800, 600, [{"lines": [_line(10, 10, ["Ahoj", "světe"])]}]),
    )

    # Roles emit <Tags>/<StructureTag> plus TAGREFS on the line; "body" is deliberately
    # not tagged, so this mixes referenced and unreferenced lines in one block.
    yield (
        "role tags + TAGREFS",
        build_alto(
            "page-2",
            800,
            600,
            [
                {
                    "lines": [
                        _line(10, 10, ["Kapitola"], role="heading"),
                        _line(10, 60, ["text"], role="body"),
                        _line(10, 110, ["12"], role="page-number"),
                    ]
                }
            ],
            layout_name="dec-B-v2",
        ),
    )

    # Page ID goes into xsd:ID, which is an NCName — sanitize_xml_id has to fold this.
    yield (
        "non-ASCII page id",
        build_alto("Příliš žluťoučký kůň.jpg", 400, 400, [{"lines": [_line(5, 5, ["kůň"])]}]),
    )

    # Coordinates outside the page must be clipped to it, not emitted as-is.
    yield (
        "out-of-bounds coordinates",
        build_alto(
            "page-3",
            200,
            200,
            [
                {
                    "lines": [
                        {
                            "hpos": -50,
                            "vpos": 190,
                            "width": 900,
                            "height": 900,
                            "words": [("přetečení", -50, 190, 900, 900)],
                        }
                    ]
                }
            ],
        ),
    )

    # Blocks without lines are dropped, so both of these yield a bare PrintSpace.
    yield "no blocks", build_alto("page-4", 300, 300, [])
    yield "blocks with no lines", build_alto("page-5", 300, 300, [{"lines": []}])

    # Single-word lines emit no <SP>; multi-word lines interleave them.
    yield (
        "single word, no SP",
        build_alto("page-6", 300, 300, [{"lines": [_line(10, 10, ["Solo"])]}]),
    )

    # Attribute values that need XML escaping rather than sanitizing.
    yield (
        "markup-hostile CONTENT",
        build_alto("page-7", 300, 300, [{"lines": [_line(10, 10, ["<a&b>", '"quoted"'])]}]),
    )


def validate(schema: xmlschema.XMLSchema, source: str, label: str, quiet: bool) -> bool:
    """Validate one source; print the outcome and return True if it is valid."""
    errors = list(schema.iter_errors(source))
    if not errors:
        if not quiet:
            print(f"{GREEN}  ok{OFF} {label}")
        return True

    print(f"{RED}FAIL{OFF} {label} — {len(errors)} schema error(s)")
    for error in errors[:MAX_ERRORS_SHOWN]:
        reason = (error.reason or "invalid").strip()
        print(f"      {reason}")
        print(f"{DIM}        at {error.path}{OFF}")
    if len(errors) > MAX_ERRORS_SHOWN:
        print(f"{DIM}      … {len(errors) - MAX_ERRORS_SHOWN} more{OFF}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "files", nargs="*", type=Path, help=f"ALTO files to validate (default: {DEFAULT_GLOB})"
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="skip the build_alto() fixtures, validate files only",
    )
    parser.add_argument("--quiet", action="store_true", help="print failures only")
    args = parser.parse_args()

    if not ALTO_XSD.is_file() or not XLINK_XSD.is_file():
        print(f"{RED}FAIL{OFF} vendored schema missing under {SCHEMA_DIR}", file=sys.stderr)
        return 1

    print(f"{CYAN}==>{OFF} Loading ALTO 4.4 schema from {ALTO_XSD.relative_to(ROOT)}")
    schema = load_schema()

    checked = 0
    failed = 0

    if args.no_generate:
        print(f"{CYAN}==>{OFF} Skipping generated fixtures (--no-generate)")
    elif not ENGINE_ALTO_PY.is_file():
        print(
            f"{CYAN}==>{OFF} Skipping generated fixtures — no engine clone at "
            f"{ENGINE_ALTO_PY.parent.parent.relative_to(ROOT)} (see README)"
        )
    else:
        print(f"{CYAN}==>{OFF} Validating build_alto() output")
        build_alto = load_engine_alto().build_alto
        for name, document in generated_cases(build_alto):
            checked += 1
            failed += not validate(schema, document, f"generated: {name}", args.quiet)

    files = args.files or sorted(ROOT.glob(DEFAULT_GLOB))
    if files:
        print(f"{CYAN}==>{OFF} Validating {len(files)} ALTO file(s) on disk")
    for path in files:
        checked += 1
        label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
        if not path.is_file():
            print(f"{RED}FAIL{OFF} {label} — not a file")
            failed += 1
            continue
        failed += not validate(schema, str(path), label, args.quiet)

    if not checked:
        print(
            f"{RED}FAIL{OFF} nothing to validate: no engine clone at ./TuzkaOCR and no "
            f"files matched {DEFAULT_GLOB}.\n"
            f"      Clone the engine (see README), run `make e2e` to "
            f"produce real output, or pass files: "
            f"`make check-alto-schema FILES='path/to/*.xml'`",
            file=sys.stderr,
        )
        return 1

    if failed:
        print(f"\n{RED}FAIL{OFF}  {failed} of {checked} document(s) violate ALTO 4.4")
        return 1

    print(f"\n{GREEN}PASS{OFF}  {checked} document(s) conform to ALTO 4.4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
