"""Tests for submit-time job validation: domain, outputs, options (D5-D7).

The parsers and ``options_errors`` are pure and always run. ``resolve_job_spec`` needs
a throwaway migrated Postgres via ``TEST_DATABASE_URL`` and skips otherwise.
"""

import json
import os
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.engines.openapi import options_schema_from_openapi
from app.services.job_spec import (
    adapter_rejection,
    clear_capabilities_cache,
    fmt_label,
    options_errors,
    parse_external_id,
    parse_options,
    parse_outputs,
    resolve_job_spec,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
ALTO_TXT = frozenset({"alto", "txt"})

# The schema domain sync derives from TuzkaOCR's OpenAPI.
SCHEMA = options_schema_from_openapi(
    json.loads((Path(__file__).parent / "fixtures" / "tuzkaocr_openapi.json").read_text())
)


# ---- parse_outputs ----


@pytest.mark.parametrize(
    ("raw", "fmt", "expected"),
    [
        (None, "multi", ALTO_TXT),
        (None, "alto", {"alto"}),
        (None, "txt", {"txt"}),
        ("alto", "multi", {"alto"}),  # outputs wins over fmt
        ("ALTO, txt", "alto", ALTO_TXT),  # stripped + lowercased
        ("geojson,json", "multi", {"geojson", "json"}),  # unknown names are fine here
        ("alto,alto", "multi", {"alto"}),
    ],
)
def test_parse_outputs(raw, fmt, expected) -> None:
    assert parse_outputs(raw, fmt) == expected


@pytest.mark.parametrize("raw", ["", " ", "alto,", ",txt", "alto,,txt"])
def test_parse_outputs_rejects_empty_items(raw) -> None:
    with pytest.raises(ValueError):
        parse_outputs(raw, "multi")


# ---- parse_options ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, {}),
        ("", {}),
        ("{}", {}),
        ('{"alto_profile": "basic"}', {"alto_profile": "basic"}),
    ],
)
def test_parse_options(raw, expected) -> None:
    assert parse_options(raw) == expected


@pytest.mark.parametrize("raw", ["[]", '"x"', "1", "{not json"])
def test_parse_options_rejects_non_objects(raw) -> None:
    with pytest.raises(ValueError):
        parse_options(raw)


# ---- fmt_label ----


def test_fmt_label() -> None:
    assert fmt_label(ALTO_TXT) == "multi"
    assert fmt_label(frozenset({"alto"})) == "alto"
    assert fmt_label(frozenset({"geojson"})) == "custom"


# ---- options_errors ----


def test_valid_options_have_no_errors() -> None:
    options = {"alto_profile": "basic", "physical_img_nr": 3, "role_classifier": True}
    assert options_errors(SCHEMA, options) == []
    assert options_errors(SCHEMA, {}) == []


def test_unknown_option_key_is_an_error() -> None:
    errors = options_errors(SCHEMA, {"colour": "red"})
    assert len(errors) == 1
    assert "colour" in errors[0]


def test_wrongly_typed_option_is_an_error() -> None:
    errors = options_errors(SCHEMA, {"physical_img_nr": "x"})
    assert len(errors) == 1
    assert errors[0].startswith("physical_img_nr:")


def test_no_schema_accepts_only_empty_options() -> None:
    assert options_errors(None, {}) == []
    assert options_errors(None, {"alto_profile": "basic"}) == ["unknown option 'alto_profile'"]


# ---- resolve_job_spec (DB) ----


@pytest.fixture
async def session():
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run these tests")
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("TEST_DATABASE_URL not reachable")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    clear_capabilities_cache()  # each test seeds its own rows
    async with factory() as s:
        await s.execute(
            text(
                "TRUNCATE backend_domains, domains, "
                "job_results, jobs, backends, users RESTART IDENTITY CASCADE"
            )
        )
        # Backend 1 serves default (with the TuzkaOCR schema) and kramarky (no schema
        # yet); disabled backend 2 is the only one serving handwritten.
        await s.execute(
            text(
                "INSERT INTO backends (id, url, enabled) VALUES "
                "(1, 'http://e1', true), (2, 'http://e2', false)"
            )
        )
        await s.execute(
            text(
                "INSERT INTO domains (id, name, options_schema) VALUES "
                "(1, 'default', CAST(:schema AS jsonb)), (2, 'kramarky', NULL), "
                "(3, 'handwritten', NULL)"
            ),
            {"schema": json.dumps(SCHEMA)},
        )
        await s.execute(
            text(
                "INSERT INTO backend_domains (backend_id, domain_id, outputs) VALUES "
                "(1, 1, '{alto,txt}'), (1, 2, '{alto,txt}'), (2, 3, '{alto,txt}')"
            )
        )
        await s.commit()
        yield s
    await engine.dispose()


async def _rejects(session, domain, outputs, options) -> str:
    with pytest.raises(HTTPException) as exc_info:
        await resolve_job_spec(session, domain, outputs, options)
    assert exc_info.value.status_code == 422
    return str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_omitted_domain_is_default(session) -> None:
    spec = await resolve_job_spec(session, None, ALTO_TXT, '{"alto_profile": "basic"}')
    assert spec.domain == "default"
    assert spec.outputs == ALTO_TXT
    assert spec.options == {"alto_profile": "basic"}


@pytest.mark.asyncio
async def test_unknown_domain(session) -> None:
    assert "nonexistent" in await _rejects(session, "nonexistent", ALTO_TXT, None)


@pytest.mark.asyncio
async def test_domain_served_only_by_disabled_backend(session) -> None:
    assert "handwritten" in await _rejects(session, "handwritten", ALTO_TXT, None)


@pytest.mark.asyncio
async def test_output_the_domain_lacks(session) -> None:
    detail = await _rejects(session, "default", frozenset({"geojson"}), None)
    assert "alto, txt" in detail  # names the domain's outputs


@pytest.mark.asyncio
async def test_unknown_option_key(session) -> None:
    assert "colour" in await _rejects(session, "default", ALTO_TXT, '{"colour": "red"}')


@pytest.mark.asyncio
async def test_wrongly_typed_option(session) -> None:
    detail = await _rejects(session, "default", ALTO_TXT, '{"physical_img_nr": "x"}')
    assert "physical_img_nr" in detail


@pytest.mark.asyncio
async def test_domain_without_schema_accepts_only_empty_options(session) -> None:
    spec = await resolve_job_spec(session, "kramarky", ALTO_TXT, None)
    assert spec.options == {}
    assert "alto_profile" in await _rejects(
        session, "kramarky", ALTO_TXT, '{"alto_profile": "basic"}'
    )


# ---- parse_external_id ----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        ("uuid:0109e8c2-d262-11e1-b9d7-0050569d679d", "uuid:0109e8c2-d262-11e1-b9d7-0050569d679d"),
        ("ABA001 p. 12", "ABA001 p. 12"),
        ("x" * 255, "x" * 255),
    ],
)
def test_parse_external_id(raw, expected) -> None:
    assert parse_external_id(raw) == expected


@pytest.mark.parametrize("raw", ["x" * 256, "line\nbreak", "nul\x00"])
def test_parse_external_id_rejects(raw) -> None:
    with pytest.raises(ValueError):
        parse_external_id(raw)


# ---- adapter_rejection (D3 hook) ----


def test_adapter_rejection_when_every_adapter_refuses() -> None:
    reason = adapter_rejection({"tuzkam"}, "maps", frozenset({"geojson"}), {"georef": False})
    assert reason == "geojson needs georef=true"


def test_adapter_rejection_none_when_one_adapter_accepts() -> None:
    adapters = {"tuzkam", "tuzkaocr"}
    assert adapter_rejection(adapters, "maps", frozenset({"geojson"}), {"georef": False}) is None
    assert adapter_rejection(set(), "maps", frozenset({"geojson"}), {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("domain", ["de\x00fault", "line\nbreak"])
async def test_unprintable_domain_is_422_before_the_lookup(domain) -> None:
    with pytest.raises(HTTPException) as exc:
        await resolve_job_spec(None, domain, frozenset({"alto"}), None)
    assert exc.value.status_code == 422
