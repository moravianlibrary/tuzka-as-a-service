"""Plumbing shared by the taas-admin commands: run an operation, report, print."""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime
from enum import StrEnum
from typing import Any

import typer
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from rich import box
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.db import make_engine, make_session_factory

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
            async with make_session_factory(engine)() as db:
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


# Tables and details go through rich: aligned to the terminal's width and colored on a
# terminal; plain and never wrapped when piped (kubectl exec without -t, | grep, | less).
_console = Console(highlight=False)


def _print(renderable: RenderableType) -> None:
    if _console.is_terminal:
        _console.print(renderable)
        return
    plain = Console(file=io.StringIO(), width=10_000, no_color=True, highlight=False)
    plain.print(renderable)
    for line in plain.file.getvalue().splitlines():  # type: ignore[attr-defined]
        typer.echo(line.rstrip())


def output(model: type[BaseModel], objs: Iterable[object], as_json: bool) -> None:
    """Print each object as a ``model`` row: a table with a column per field, or a JSON
    list with ``--json``. The table leaves out a column no row has a value in (a user's
    rate overrides, say), since a column of dashes says nothing; ``--json`` keeps all."""
    rows = [model.model_validate(obj, from_attributes=True) for obj in objs]
    if as_json:
        typer.echo(json.dumps([row.model_dump(mode="json") for row in rows], indent=2))
        return
    if not rows:
        _print(Text("(none)", style="dim"))
        return

    columns = [c for c in model.model_fields if any(_has_value(getattr(r, c)) for r in rows)]
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, header_style="bold")
    for column in columns:
        numeric = all(_is_number(getattr(r, column)) for r in rows)
        table.add_column(_title(column), justify="right" if numeric else "left")
    for row in rows:
        table.add_row(*(_cell(getattr(row, c)) for c in columns))
    _print(table)


def show(obj: BaseModel, as_json: bool) -> None:
    """Print one object field by field, or as JSON with ``--json``. A field holding a
    model (a user's limit overrides) is a section of its own below the fields, and so is
    one holding a list of models (a job's attempts), as a table."""
    if as_json:
        typer.echo(obj.model_dump_json(indent=2))
        return

    fields = Table.grid(padding=(0, 2))
    fields.add_column(style="bold")
    fields.add_column()
    sections: list[tuple[str, BaseModel | list[BaseModel]]] = []
    for name in type(obj).model_fields:
        value = getattr(obj, name)
        if isinstance(value, BaseModel) or (
            isinstance(value, list) and value and isinstance(value[0], BaseModel)
        ):
            sections.append((name, value))
            continue
        fields.add_row(_title(name), _cell(value))
    _print(fields)
    for name, section in sections:
        _print(Text(f"\n{_title(name)}", style="bold"))
        if isinstance(section, BaseModel):
            show(section, as_json=False)
        else:
            output(type(section[0]), section, as_json=False)


def _title(field: str) -> str:
    return field.replace("_", " ")


def _has_value(value: Any) -> bool:
    return value is not None and value != [] and value != {}


def _is_number(value: Any) -> bool:
    return value is None or (isinstance(value, int | float) and not isinstance(value, bool))


def _cell(value: Any) -> Text:
    if value is None or value == [] or value == {}:
        return Text("-", style="dim")
    if isinstance(value, bool):
        return Text("yes", style="green") if value else Text("no", style="red")
    if isinstance(value, datetime):
        return Text(value.strftime("%Y-%m-%d %H:%M:%S"))
    if isinstance(value, StrEnum):
        return Text(value.value)
    if isinstance(value, list | tuple | set | frozenset):
        return Text(", ".join(str(v) for v in value))
    if isinstance(value, dict):
        return Text(" ".join(f"{k}={json.dumps(v)}" for k, v in value.items()))
    return Text(str(value))
