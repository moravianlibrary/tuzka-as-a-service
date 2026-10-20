"""``taas-admin``: users, backends, domains, runtime config and failed jobs.

Runs inside the api image (``docker compose exec -T api taas-admin …``,
``kubectl exec deploy/<release>-api -- taas-admin …``) with the same environment as
the API, so it reaches the same database.
"""

import typer

from app.cli import backend, config, domain, job, user

app = typer.Typer(no_args_is_help=True, help=__doc__)
app.add_typer(user.app, name="user", help="API users, their keys, limits and scopes.")
app.add_typer(backend.app, name="backend", help="OCR engines taas dispatches to.")
app.add_typer(domain.app, name="domain", help="Domains: labels and legacy engine ids.")
app.add_typer(config.app, name="config", help="Runtime config (the config table).")
app.add_typer(job.app, name="job", help="Inspect jobs (failed ones are kept longest).")

if __name__ == "__main__":
    app()
