"""Tests for scoped API keys: the pure check (always run)."""

import pytest

from app.services.scopes import Principal, check_scope, in_scope_outputs, visible_outputs

ALTO_TXT = frozenset({"alto", "txt"})


@pytest.mark.parametrize(
    ("principal", "domain", "outputs", "allowed"),
    [
        (Principal("a"), "maps", frozenset({"geojson"}), True),  # unscoped: everything
        (Principal("a", frozenset({"default"})), "default", ALTO_TXT, True),
        (Principal("a", frozenset({"default"})), "maps", ALTO_TXT, False),
        (Principal("a", None, frozenset({"alto"})), "default", frozenset({"alto"}), True),
        (Principal("a", None, frozenset({"alto"})), "default", ALTO_TXT, False),
        (Principal("a", frozenset({"default"}), frozenset()), "default", ALTO_TXT, False),
    ],
)
def test_check_scope(principal, domain, outputs, allowed) -> None:
    assert (check_scope(principal, domain, outputs) is None) is allowed


def test_denied_outputs_are_named() -> None:
    message = check_scope(Principal("a", None, frozenset({"alto"})), "default", ALTO_TXT)
    assert message is not None and "txt" in message


def test_in_scope_outputs() -> None:
    assert in_scope_outputs(Principal("a"), ALTO_TXT) == ALTO_TXT
    assert in_scope_outputs(Principal("a", None, frozenset({"alto"})), ALTO_TXT) == {"alto"}


def test_visible_outputs() -> None:
    both = frozenset({"alto", "txt"})
    assert visible_outputs(Principal("u"), "maps", both) == both
    scoped = Principal("u", allowed_domains=frozenset({"default"}))
    assert visible_outputs(scoped, "maps", both) == frozenset()
    assert visible_outputs(scoped, "default", both) == both
    txt_only = Principal("u", allowed_outputs=frozenset({"txt"}))
    assert visible_outputs(txt_only, "default", both) == frozenset({"txt"})
    geo_only = Principal("u", allowed_outputs=frozenset({"geojson"}))
    assert visible_outputs(geo_only, "default", both) == frozenset()
