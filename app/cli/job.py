"""``taas-admin job …``"""

import uuid

import typer

from app.cli.common import JsonOption, output, run
from app.schemas.api.job import FailedJob, JobDetail
from app.services import admin_ops

app = typer.Typer(no_args_is_help=True)


@app.command("list-failed")
def list_failed(limit: int = typer.Option(50), as_json: bool = JsonOption) -> None:
    """The most recently failed jobs."""
    jobs = run(lambda db: admin_ops.list_failed_jobs(db, limit))
    output(FailedJob, jobs, as_json)


@app.command()
def show(job_id: uuid.UUID) -> None:
    """One job with every failed attempt (stage, backend, HTTP status, error, retry)."""
    job = run(lambda db: admin_ops.get_job(db, job_id))
    typer.echo(JobDetail.model_validate(job, from_attributes=True).model_dump_json(indent=2))
