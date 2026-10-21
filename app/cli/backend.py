"""``taas-admin backend …``"""

import json

import typer

from app.cli.common import JsonOption, build, output, row, run, secret_from_env, settings
from app.core import admin_ops
from app.core.schemas.backend import BackendCreate, BackendUpdate

app = typer.Typer(no_args_is_help=True)

COLUMNS = (
    "id",
    "url",
    "label",
    "adapter",
    "enabled",
    "max_inflight",
    "priority",
    "device",
    "managed",
    "created_at",
)

ApiKeyEnv = typer.Option(
    None, help="Env var holding the engine's API key (never pass it in argv); empty clears it."
)


def _print(backend: object) -> None:
    output([row(backend, COLUMNS)], COLUMNS, as_json=False)


@app.command("list")
def list_backends(as_json: bool = JsonOption) -> None:
    """List backends."""
    output((row(b, COLUMNS) for b in run(admin_ops.list_backends)), COLUMNS, as_json)


@app.command()
def upsert(
    url: str = typer.Option(...),
    label: str | None = typer.Option(None),
    api_key_env: str | None = ApiKeyEnv,
    max_inflight: int = typer.Option(4),
    device: str = typer.Option("cpu"),
    adapter: str = typer.Option("tuzkaocr"),
    managed: bool = typer.Option(False, help="Registered by the chart (shown, not edited)."),
    declared: str | None = typer.Option(
        None, help='Capabilities to assume until the engine is seen, as JSON: {"domain": ["alto", …]}.'
    ),
) -> None:
    """Create the backend with this URL, or update it. Idempotent (the Helm hook runs it
    on every deploy); priority is left alone."""
    body = build(
        BackendCreate,
        url=url,
        label=label,
        api_key=secret_from_env(api_key_env),
        max_inflight=max_inflight,
        device=device,
        adapter=adapter,
        managed=managed,
        declared=json.loads(declared) if declared else None,
    )
    secret = settings().key_encryption_secret
    _print(run(lambda db: admin_ops.upsert_backend(db, body, secret)))


@app.command()
def update(
    backend_id: int,
    url: str | None = typer.Option(None),
    label: str | None = typer.Option(None),
    api_key_env: str | None = ApiKeyEnv,
    max_inflight: int | None = typer.Option(None),
    priority: int | None = typer.Option(None),
    device: str | None = typer.Option(None),
    adapter: str | None = typer.Option(None),
) -> None:
    """Change some fields of a backend; options left out stay as they are."""
    fields = {
        k: v
        for k, v in {
            "url": url,
            "label": label,
            "max_inflight": max_inflight,
            "priority": priority,
            "device": device,
            "adapter": adapter,
        }.items()
        if v is not None
    }
    if api_key_env is not None:
        fields["api_key"] = secret_from_env(api_key_env)
    if not fields:
        typer.echo("error: nothing to change", err=True)
        raise typer.Exit(2)
    _set(backend_id, **fields)


def _set(backend_id: int, **fields: object) -> None:
    body = build(BackendUpdate, **fields)
    secret = settings().key_encryption_secret
    _print(run(lambda db: admin_ops.update_backend(db, backend_id, body, secret)))


@app.command()
def enable(backend_id: int) -> None:
    """Put the backend back into rotation."""
    _set(backend_id, enabled=True)


@app.command()
def disable(backend_id: int) -> None:
    """Take the backend out of rotation without deleting it."""
    _set(backend_id, enabled=False)


@app.command()
def delete(backend_id: int) -> None:
    """Delete a backend (refused while jobs still reference it; disable it instead)."""
    run(lambda db: admin_ops.delete_backend(db, backend_id))
    typer.echo(f"deleted backend {backend_id}")
