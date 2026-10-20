"""``taas-admin user …``"""

import typer

from app.cli.common import JsonOption, build, output, row, run, secret_from_env
from app.core import admin_ops
from app.core.schemas.user import UserCreate, UserUpdate

app = typer.Typer(no_args_is_help=True)

COLUMNS = (
    "username",
    "active",
    "priority",
    "created_at",
    "rate_submit_per_minute",
    "burst_submit",
    "rate_query_per_minute",
    "burst_query",
    "rate_ws_per_minute",
    "burst_ws",
    "allowed_domains",
    "allowed_outputs",
)


@app.command("list")
def list_users(as_json: bool = JsonOption) -> None:
    """List users, newest first."""
    users = run(admin_ops.list_users)
    output((row(u, COLUMNS) for u in users), COLUMNS, as_json)


@app.command()
def create(username: str) -> None:
    """Create a user and print their API key. It's shown only this once."""
    body = build(UserCreate, username=username)
    key = run(lambda db: admin_ops.create_user(db, body.username))
    typer.echo(key)


@app.command()
def delete(username: str) -> None:
    """Delete a user (refused while they still have jobs; disable them instead)."""
    run(lambda db: admin_ops.delete_user(db, username))
    typer.echo(f"deleted {username}")


@app.command("rotate-key")
def rotate_key(username: str) -> None:
    """Give the user a new API key (printed once); the old one stops working."""
    typer.echo(run(lambda db: admin_ops.rotate_key(db, username)))


@app.command("set-key")
def set_key(
    username: str,
    key_env: str = typer.Option(..., help="Env var holding the new key (never pass it in argv)."),
) -> None:
    """Set the user's API key to a chosen value."""
    key = secret_from_env(key_env)
    assert key is not None
    run(lambda db: admin_ops.set_key(db, username, key))
    typer.echo(f"key updated for {username}")


def _update(username: str, **fields: object) -> None:
    body = build(UserUpdate, **fields)
    result = run(lambda db: admin_ops.update_user(db, username, body))
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def enable(username: str) -> None:
    """Let the user's key authenticate again."""
    _update(username, active=True)


@app.command()
def disable(username: str) -> None:
    """Stop the user's key from authenticating, keeping the user and their jobs."""
    _update(username, active=False)


@app.command("set-priority")
def set_priority(username: str, priority: int) -> None:
    """Set the user's queue priority (higher is dispatched first)."""
    _update(username, priority=priority)


@app.command("set-limits")
def set_limits(
    username: str,
    submit_per_minute: int | None = typer.Option(None),
    submit_burst: int | None = typer.Option(None),
    query_per_minute: int | None = typer.Option(None),
    query_burst: int | None = typer.Option(None),
    ws_per_minute: int | None = typer.Option(None),
    ws_burst: int | None = typer.Option(None),
    clear: list[str] = typer.Option(
        [], help="Class to reset to the global default: submit, query or ws (repeatable)."
    ),
) -> None:
    """Override the user's rate limits; options left out stay as they are."""
    fields: dict[str, object] = {}
    for cls, per_minute, burst in (
        ("submit", submit_per_minute, submit_burst),
        ("query", query_per_minute, query_burst),
        ("ws", ws_per_minute, ws_burst),
    ):
        if cls in clear:
            fields[f"rate_{cls}_per_minute"] = None
            fields[f"burst_{cls}"] = None
        if per_minute is not None:
            fields[f"rate_{cls}_per_minute"] = per_minute
        if burst is not None:
            fields[f"burst_{cls}"] = burst
    if not fields:
        typer.echo("error: nothing to change", err=True)
        raise typer.Exit(2)
    _update(username, **fields)


def _names(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


@app.command("set-scopes")
def set_scopes(
    username: str,
    domains: str | None = typer.Option(None, help="Comma-separated domains the key may use."),
    outputs: str | None = typer.Option(None, help="Comma-separated outputs it may request."),
    all_: bool = typer.Option(False, "--all", help="Lift every limit (the default)."),
) -> None:
    """Scope the user's key: an omitted option means "any". Takes effect within ~10 s."""
    if all_ and (domains or outputs):
        typer.echo("error: --all can't be combined with --domains/--outputs", err=True)
        raise typer.Exit(2)
    if not all_ and not (domains or outputs):
        typer.echo("error: give --domains and/or --outputs, or --all", err=True)
        raise typer.Exit(2)
    user = run(lambda db: admin_ops.set_scopes(db, username, _names(domains), _names(outputs)))
    output([row(user, COLUMNS)], COLUMNS, as_json=False)
