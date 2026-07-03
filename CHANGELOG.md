# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Master-key authentication now uses a constant-time comparison (`hmac.compare_digest`),
  removing a timing side-channel on the secret.
- An invalid or missing master key is now rejected with `401 Unauthorized` (was `403`),
  matching the documented API contract.

### Added

- Even dispatch within a priority tier: the submit worker now deals jobs round-robin
  across the healthy backends of a priority tier (highest tier first), respecting each
  backend's free capacity and served domains, instead of filling one backend to
  capacity before the next. Spreads load so results return with more parallelism.
- Event-driven dispatch: the submit worker now blocks on a Redis wakeup signal
  (`signal_submit`) raised whenever a job becomes pending or a backend slot frees, so
  fast jobs refill slots immediately instead of waiting out the fixed tick. The tick
  becomes a max idle-wait floor.
- Quality gate: `make check` (lint + `mypy --strict` types + version drift + unit tests),
  plus `make typecheck`, `make test-unit`, and `make version-check`.
- `make set-version VERSION=x.y.z` propagates the version into every manifest
  (`VERSION`, both `pyproject.toml`s, `app/main.py`, `compat/app/main.py`,
  Helm `Chart.yaml`); `make version-check` fails on drift.
- Committed `uv.lock` for reproducible installs; dev tooling runs via `uv`.
- CI: secret scanning (gitleaks) and dependency CVE audit (pip-audit).

### Changed

- Job submit now validates `uuid` and `fmt` at the boundary (parsed as `UUID` /
  `Literal`), so an invalid value returns `422` instead of `400`. List endpoints cap
  `limit` at 200 (`limit`/`offset` are range-validated). Backend `device` is validated
  as `cpu`/`gpu` at the schema boundary.
- Index `jobs.backend_id` and add a `ck_jobs_status` CHECK constraint pinning
  `jobs.status` to the four lifecycle values (migration 010). ORM relationships now
  use `lazy="raise"`, so related rows must be loaded explicitly (no accidental N+1).
- The whole codebase now passes `mypy --strict` and an expanded Ruff rule set
  (`B` bugbear, `SIM` simplify); all `app/` and `compat/` code is fully type-annotated.
- `alembic/env.py` imports every model via the `app.models` package so
  `Base.metadata` is complete — autogenerate can no longer propose dropping the
  `domains`, `backend_domains`, or `config` tables.

### Fixed

- Worker robustness: loop errors now log full tracebacks (`logger.exception`); a failed
  domain sync retries next tick instead of being locked out; `storage.delete_objects`
  iterates delete errors correctly (was an unreachable `async for`) and logs them
  instead of `print`; fan-out dispatch/harvest no longer let one failure cancel
  siblings; workers close their engine/redis/DB clients on shutdown.
- Corrected the drifted `compat` service version (`0.5.2` → in step with the main
  version) via the new single-source version propagation.
