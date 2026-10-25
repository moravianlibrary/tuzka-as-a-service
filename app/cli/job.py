"""``taas-admin job …``"""

import json
import uuid

import typer

from app.cli.common import JsonOption, output, row, run
from app.services import admin_ops

app = typer.Typer(no_args_is_help=True)

# TODO(rrandiak): Use pydantic model
COLUMNS = ("id", "username", "external_id", "domain", "backend_id", "finished_at", "error")


@app.command("list-failed")
def list_failed(limit: int = typer.Option(50), as_json: bool = JsonOption) -> None:
    """The most recently failed jobs."""
    jobs = run(lambda db: admin_ops.list_failed_jobs(db, limit))
    output((row(j, COLUMNS) for j in jobs), COLUMNS, as_json)


@app.command()
def show(job_id: uuid.UUID) -> None:
    """One job with every failed attempt (stage, backend, HTTP status, error, retry)."""
    job = run(lambda db: admin_ops.get_job(db, job_id))
    # TODO(rrandiak): Use pydantic model
    fields = (
        "id",
        "username",
        "external_id",
        "status",
        "domain",
        "outputs",
        "options",
        "backend_id",
        "engine_version",
        "requeues",
        "submitted_at",
        "dispatched_at",
        "started_at",
        "finished_at",
        "error",
    )
    data = {f: getattr(job, f, None) for f in fields}
    data["attempts"] = job.attempts
    typer.echo(json.dumps(data, indent=2, default=str))
