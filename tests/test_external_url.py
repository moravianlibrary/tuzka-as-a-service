"""Pure-function unit tests for the per-user external catalog link — no DB needed.

Covers render_external_url: the {UUID} placeholder substitution (case-insensitive)
that turns a user's external_url_template + a job's external_id into a catalog URL."""

from uuid import UUID

from app.schemas.job import render_external_url

EXTERNAL_ID = UUID("12345678-1234-5678-1234-567812345678")


def test_render_substitutes_upper_placeholder():
    url = render_external_url("https://cat.example/uuid/{UUID}", EXTERNAL_ID)
    assert url == f"https://cat.example/uuid/{EXTERNAL_ID}"


def test_render_substitutes_lower_placeholder():
    # {uuid} still works — the substitution is case-insensitive.
    url = render_external_url("https://cat.example/uuid/{uuid}", EXTERNAL_ID)
    assert url == f"https://cat.example/uuid/{EXTERNAL_ID}"


def test_render_replaces_every_occurrence():
    url = render_external_url("{UUID}?ref={uuid}", EXTERNAL_ID)
    assert url == f"{EXTERNAL_ID}?ref={EXTERNAL_ID}"


def test_render_none_when_no_template():
    assert render_external_url(None, EXTERNAL_ID) is None
    assert render_external_url("", EXTERNAL_ID) is None


def test_render_none_when_no_external_id():
    assert render_external_url("https://cat.example/uuid/{UUID}", None) is None
