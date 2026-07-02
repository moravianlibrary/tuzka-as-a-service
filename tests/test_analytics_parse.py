"""Pure-function unit tests for the analytics helpers — no DB or Redis needed.

Covers ALTO parsing, the duration helper, the breakdown bucket-count cap, and the
ALTO category -> SQL range mapping. The DB-backed write/endpoint tests live in
test_analytics_db.py (gated on TEST_DATABASE_URL)."""

from datetime import datetime

from app.routers.dashboard import _alto_range, _bucket_count
from app.services.analytics import _dur_s, parse_alto

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


def test_bucket_count_is_inclusive_and_uses_granularity_divisor():
    frm = datetime(2026, 1, 1)
    # 10 full days -> 10 day-buckets + 1 (inclusive of the start bucket) = 11
    assert _bucket_count(frm, datetime(2026, 1, 11), "day") == 11
    # Same span at hour granularity: 10*24 hours + 1
    assert _bucket_count(frm, datetime(2026, 1, 11), "hour") == 241
    # Unknown granularity falls back to day divisor
    assert _bucket_count(frm, datetime(2026, 1, 11), "bogus") == 11


def test_bucket_count_negative_span_clamped():
    frm = datetime(2026, 1, 11)
    to = datetime(2026, 1, 1)
    assert _bucket_count(frm, to, "day") == 1


def test_alto_range_bounded_category():
    assert _alto_range("normal", "alto_lines") == " AND alto_lines BETWEEN 16 AND 60"
    assert _alto_range("simple", "alto_blocks") == " AND alto_blocks BETWEEN 1 AND 2"
    assert _alto_range("normal", "alto_chars") == " AND alto_chars BETWEEN 500 AND 3000"


def test_alto_range_open_ended_category():
    # Categories with no upper bound become a >= clause.
    assert _alto_range("very_dense", "alto_lines") == " AND alto_lines >= 301"
    assert _alto_range("fragmented", "alto_blocks") == " AND alto_blocks >= 31"
    assert _alto_range("rich", "alto_chars") == " AND alto_chars >= 3001"


def test_alto_range_empty_category_is_exact_zero():
    assert _alto_range("empty", "alto_lines") == " AND alto_lines BETWEEN 0 AND 0"


def test_alto_range_unknown_or_none_returns_empty_string():
    assert _alto_range(None, "alto_lines") == ""
    assert _alto_range("nonsense", "alto_lines") == ""
    # A valid category but wrong column also yields nothing.
    assert _alto_range("simple", "alto_lines") == ""
