"""Signed, short-lived session token for the admin dashboard.

Lets a verified master-key holder stay authenticated across page refreshes via
an httpOnly cookie, without the raw master key ever being stored in the
browser. The token is an HMAC of an expiry timestamp keyed by the master key,
so only the server (which knows the master key) can mint or verify one, and the
token itself carries no secret.
"""

import hashlib
import hmac
import time

COOKIE_NAME = "taas_dash"
DEFAULT_TTL = 3600  # seconds


def _sig(secret: str, exp: int) -> str:
    return hmac.new(secret.encode(), str(exp).encode(), hashlib.sha256).hexdigest()


def issue(secret: str, ttl: int = DEFAULT_TTL) -> str:
    exp = int(time.time()) + ttl
    return f"{exp}.{_sig(secret, exp)}"


def verify(secret: str, token: str | None) -> bool:
    if not secret or not token or "." not in token:
        return False
    exp_str, sig = token.rsplit(".", 1)
    if not exp_str.isdigit() or int(exp_str) < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sig(secret, int(exp_str)))
