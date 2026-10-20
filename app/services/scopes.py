"""What an API key may ask for: a user's allowed domains and outputs (roadmap B).

A user has one key, so the scope lives on the user. ``None`` means everything, which
is what every user had before scopes existed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """The authenticated caller."""

    username: str
    allowed_domains: frozenset[str] | None = None
    allowed_outputs: frozenset[str] | None = None


def check_scope(principal: Principal, domain: str, outputs: frozenset[str]) -> str | None:
    """Why the key may not submit this job, or None if it may."""
    if principal.allowed_domains is not None and domain not in principal.allowed_domains:
        return f"Your API key isn't allowed to use domain '{domain}'"
    if principal.allowed_outputs is not None:
        denied = sorted(outputs - principal.allowed_outputs)
        if denied:
            return f"Your API key isn't allowed to request outputs: {', '.join(denied)}"
    return None


def in_scope_outputs(principal: Principal, outputs: frozenset[str]) -> frozenset[str]:
    """The part of ``outputs`` the key may request (for listings like /domains)."""
    if principal.allowed_outputs is None:
        return outputs
    return outputs & principal.allowed_outputs
