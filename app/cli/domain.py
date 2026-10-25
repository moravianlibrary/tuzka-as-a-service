"""``taas-admin domain …``"""

import typer

from app.cli.common import JsonOption, output, row, run
from app.services import admin_ops

app = typer.Typer(no_args_is_help=True)

# TODO(rrandiak): Use pydantic model
COLUMNS = (
    "name",
    "label",
    "description",
    "legacy_engine_id",
    "max_upload_bytes",
    "running_timeout_seconds",
)


@app.command("list")
def list_domains(as_json: bool = JsonOption) -> None:
    """List the domains engines have reported."""
    output((row(d, COLUMNS) for d in run(admin_ops.list_domains)), COLUMNS, as_json)


@app.command("set-label")
def set_label(name: str, label: str, description: str | None = typer.Option(None)) -> None:
    """Set the domain's display label (and optionally its description)."""
    fields: dict[str, object] = {"label": label}
    if description is not None:
        fields["description"] = description
    domain = run(lambda db: admin_ops.update_domain(db, name, **fields))
    output([row(domain, COLUMNS)], COLUMNS, as_json=False)


@app.command("set-legacy-id")
def set_legacy_id(
    name: str,
    engine_id: int | None = typer.Argument(None, help="Omit to remove the legacy id."),
) -> None:
    """Set the engine id the legacy (PERO) compat API exposes for this domain."""
    domain = run(lambda db: admin_ops.update_domain(db, name, legacy_engine_id=engine_id))
    output([row(domain, COLUMNS)], COLUMNS, as_json=False)


@app.command("set-limits")
def set_limits(
    name: str,
    max_upload_bytes: int | None = typer.Option(None, min=1, help="Largest page upload."),
    running_timeout_seconds: int | None = typer.Option(
        None, min=1, help="How long a page may run before it's failed."
    ),
    clear: bool = typer.Option(False, "--clear", help="Back to the global limits."),
) -> None:
    """Per-domain limits (e.g. maps: big scans, slow engine); unset = the global ones."""
    if clear:
        fields: dict[str, object] = {"max_upload_bytes": None, "running_timeout_seconds": None}
    else:
        fields = {
            k: v
            for k, v in {
                "max_upload_bytes": max_upload_bytes,
                "running_timeout_seconds": running_timeout_seconds,
            }.items()
            if v is not None
        }
    if not fields:
        typer.echo(
            "error: give --max-upload-bytes and/or --running-timeout-seconds, or --clear",
            err=True,
        )
        raise typer.Exit(2)
    domain = run(lambda db: admin_ops.update_domain(db, name, **fields))
    output([row(domain, COLUMNS)], COLUMNS, as_json=False)
