"""``taas-admin config …``"""

import json
from typing import Any

import typer
from pydantic import BaseModel

from app.cli.common import JsonOption, output, run
from app.services import admin_ops

app = typer.Typer(no_args_is_help=True)


class ConfigValue(BaseModel):
    key: str
    value: Any


def _parse(value: str) -> Any:
    """Values are JSON (numbers, booleans, objects); anything else is a plain string."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


@app.command("list")
def list_config(as_json: bool = JsonOption) -> None:
    """List every config key that has a value (unset keys use their built-in default)."""
    values = run(admin_ops.get_config)
    output(ConfigValue, (ConfigValue(key=k, value=v) for k, v in values.items()), as_json)


@app.command()
def get(key: str) -> None:
    """Print one config value (as JSON)."""
    values = run(admin_ops.get_config)
    if key not in values:
        typer.echo(f"error: {key} is not set (the built-in default applies)", err=True)
        raise typer.Exit(1)
    typer.echo(json.dumps(values[key]))


@app.command("set")
def set_value(key: str, value: str) -> None:
    """Set a config value, e.g. ``jobs.max_requeues 5``. Workers see it within ~10 s.
    An unknown key or a value of the wrong type is refused."""
    run(lambda db: admin_ops.set_config(db, {key: _parse(value)}))
    typer.echo(f"{key} = {json.dumps(_parse(value))}")
