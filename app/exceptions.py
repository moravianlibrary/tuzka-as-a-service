"""Typed HTTP exceptions.

Subclass FastAPI's ``HTTPException`` so handlers raise a specific, self-describing error
(a fixed status code + a default message) instead of scattering raw ``status_code=``
literals across the routers. The status lives with the exception class, in one place.
"""

from fastapi import HTTPException


class BadRequest(HTTPException):
    def __init__(self, detail: str = "Bad request") -> None:
        super().__init__(status_code=400, detail=detail)


class Unauthorized(HTTPException):
    def __init__(self, detail: str = "Unauthorized") -> None:
        super().__init__(status_code=401, detail=detail)


class NotFound(HTTPException):
    def __init__(self, detail: str = "Not found") -> None:
        super().__init__(status_code=404, detail=detail)


class Conflict(HTTPException):
    def __init__(self, detail: str = "Conflict") -> None:
        super().__init__(status_code=409, detail=detail)


class Unprocessable(HTTPException):
    def __init__(self, detail: str = "Unprocessable entity") -> None:
        super().__init__(status_code=422, detail=detail)


class RateLimited(HTTPException):
    def __init__(self, retry_after: int, detail: str = "Rate limit exceeded") -> None:
        super().__init__(status_code=429, detail=detail, headers={"Retry-After": str(retry_after)})


class NotReady(HTTPException):
    """202 — the async job isn't finished; the client should poll again later."""

    def __init__(self, detail: str = "Not ready yet") -> None:
        super().__init__(status_code=202, detail=detail)
