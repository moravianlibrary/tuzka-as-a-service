"""``taas-admin domain …``"""

import typer

from app.cli.common import JsonOption, output, row, run
from app.core import admin_ops

app = typer.Typer(no_args_is_help=True)

COLUMNS = ("name", "label", "description", "legacy_engine_id")


@app.command("list")
def list_domains(as_json: bool = JsonOption) -> None:
    """List the domains engines have reported."""
    output((row(d, COLUMNS) for d in run(admin_ops.list_domains)), COLUMNS, as_json)


@app.command("set-label")
def set_label(
    name: str, label: str, description: str | None = typer.Option(None)
) -> None:
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
