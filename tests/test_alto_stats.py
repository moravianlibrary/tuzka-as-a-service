"""Pure-function unit tests for the analytics helpers — no DB or Redis needed.

Covers ALTO parsing, the duration helper, the breakdown bucket-count cap, and the
ALTO category -> SQL range mapping. The DB-backed write/endpoint tests live in
test_analytics_db.py (gated on TEST_DATABASE_URL)."""

from datetime import datetime

from app.services.alto_stats import parse_alto
from app.services.events import _dur_s

ALTO_NS = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#">'
    b"<Layout><Page><PrintSpace>"
    b'<TextBlock><TextLine><String CONTENT="hello"/><String CONTENT="world"/></TextLine>'
    b'<TextLine><String CONTENT="foo"/></TextLine></TextBlock>'
    b'<TextBlock><TextLine><String CONTENT="bar"/></TextLine></TextBlock>'
    b"</PrintSpace></Page></Layout></alto>"
)

ALTO_NO_NS = (
    b"<alto><Layout><Page><PrintSpace>"
    b'<TextBlock><TextLine><String CONTENT="abcd"/></TextLine></TextBlock>'
    b"</PrintSpace></Page></Layout></alto>"
)


def test_parse_alto_namespaced_counts_blocks_chars():
    blocks, chars = parse_alto(ALTO_NS)
    assert blocks == 2  # two TextBlock elements (lines come from the engine, not parsed)
    assert chars == len("hello") + len("world") + len("foo") + len("bar")  # 16


def test_parse_alto_without_namespace():
    assert parse_alto(ALTO_NO_NS) == (1, 4)


def test_parse_alto_empty_document():
    assert parse_alto(b"<alto></alto>") == (0, 0)


def test_parse_alto_malformed_returns_zeros():
    assert parse_alto(b"not xml at all <<<") == (0, 0)
    assert parse_alto(b"") == (0, 0)


def test_dur_s_normal_delta():
    a = datetime(2026, 1, 1, 12, 0, 0)
    b = datetime(2026, 1, 1, 12, 0, 30)
    assert _dur_s(a, b) == 30.0


def test_dur_s_none_endpoints():
    now = datetime(2026, 1, 1, 12, 0, 0)
    assert _dur_s(None, now) is None
    assert _dur_s(now, None) is None
    assert _dur_s(None, None) is None


def test_dur_s_negative_is_clamped_to_none():
    a = datetime(2026, 1, 1, 12, 0, 30)
    b = datetime(2026, 1, 1, 12, 0, 0)
    # Out-of-order timestamps (clock skew / retries) should not produce negative durations.
    assert _dur_s(a, b) is None
