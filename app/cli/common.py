"""Plumbing shared by the taas-admin commands: run an operation, report, print."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Iterable, Sequence
from contextlib import suppress
from datetime import datetime
from typing import Any

import typer
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.db import make_engine

# Exit codes: 1 = the operation was refused (not found, conflict), 2 = bad input.
EXIT_REFUSED = 1
EXIT_INVALID = 2

JsonOption = typer.Option(False, "--json", help="Print JSON instead of a table.")


def settings() -> Settings:
    return Settings()


def run[T](op: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Run one admin operation in its own session, and turn its errors into a message
    and an exit code instead of a traceback."""

    async def _main() -> T:
        engine = make_engine(settings())
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                return await op(db)
        finally:
            await engine.dispose()

    try:
        return asyncio.run(_main())
    except HTTPException as e:  # NotFound, Conflict, BadRequest from admin_ops
        typer.echo(f"error: {e.detail}", err=True)
        raise typer.Exit(EXIT_INVALID if e.status_code == 400 else EXIT_REFUSED) from e


def build[T: BaseModel](model: type[T], **fields: Any) -> T:
    """Validate CLI input with the same pydantic schema the API used; a bad value
    prints ``error: <field>: <message>`` and exits 2."""
    try:
        return model(**fields)
    except ValidationError as e:
        for err in e.errors():
            where = ".".join(str(p) for p in err["loc"]) or "input"
            typer.echo(f"error: {where}: {err['msg']}", err=True)
        raise typer.Exit(EXIT_INVALID) from e


def secret_from_env(var: str | None) -> str | None:
    """Read a secret from the named env var, so it never shows up in argv or shell
    history. None when no variable was named."""
    if var is None:
        return None
    value = os.environ.get(var)
    if value is None:
        typer.echo(f"error: environment variable {var} is not set", err=True)
        raise typer.Exit(EXIT_INVALID)
    return value


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, (list, tuple, set, frozenset)):
        return ",".join(str(v) for v in value) or "-"
    return str(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    with suppress(TypeError):
        json.dumps(value)
        return value
    return str(value)


def output(rows: Iterable[dict[str, Any]], columns: Sequence[str], as_json: bool) -> None:
    """Print rows as an aligned table, or as a JSON list with ``--json``."""
    rows = list(rows)
    if as_json:
        typer.echo(json.dumps([{c: _jsonable(r.get(c)) for c in columns} for r in rows], indent=2))
        return
    cells = [[_cell(r.get(c)) for c in columns] for r in rows]
    widths = [max([len(c), *(len(row[i]) for row in cells)]) for i, c in enumerate(columns)]
    typer.echo("  ".join(c.upper().ljust(w) for c, w in zip(columns, widths, strict=True)).rstrip())
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths, strict=True)).rstrip())


def row(obj: Any, columns: Sequence[str]) -> dict[str, Any]:
    return {c: getattr(obj, c, None) for c in columns}
