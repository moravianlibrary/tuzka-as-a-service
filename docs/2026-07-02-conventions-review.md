# Coding-conventions review — 2026-07-02

Review of the `taas` application code against the personal coding conventions
(layers: **foundations · security · engineering · python · api · persistence · backend ·
makefiles**).

**Scope:** `app/` (Python), the dashboard (`app/static/*.js|html|css`), and repo tooling
(`pyproject.toml`, `Makefile`, `alembic/`). Ops assets (Helm, `bench/`, `deploy/` scripts)
were excluded as out of scope for the code standard.

**Verdict: needs changes.** No blocking *correctness* bug, but the engineering quality gate
is largely absent and two API issues should land before this is "ready".

---

## Must-fix

| # | Location | Finding | Convention | Fix |
|---|----------|---------|------------|-----|
| 1 | `Makefile` | No `check` aggregator, no `typecheck` target, and `test:` (line 76) runs the **e2e smoke test** — nothing wires the `pytest` suite in `tests/` into Make. | makefiles.md · engineering.md · python.md | Add `test` (pytest), `format-check`, `typecheck`, and `check: format-check lint typecheck test`. |
| 2 | `pyproject.toml:39` | ruff `select = ["E","F","I","W","UP"]` is missing **`B` (bugbear)** and **`SIM` (simplify)**. | python.md | `select = ["E","F","I","W","UP","B","SIM"]`. |
| 3 | project-wide | No type checker configured or run. | python.md | Add `mypy --strict` (or pyright) and put it in `check`. |
| 4 | `pyproject.toml` | No committed lockfile; deps are `>=` ranges only. | engineering.md · security.md (supply chain) | Adopt `uv`; commit `uv.lock`. |
| 5 | `VERSION`, `pyproject.toml:3`, `app/main.py:74` | Version `0.5.2` hand-copied in 3 places, no propagation/drift guard; no `CHANGELOG.md`. | engineering.md | Single version source + `make set-version`; add `CHANGELOG.md`. |
| 6 | `app/routers/jobs.py:316-319` | `list_jobs` `limit`/`offset` are unbounded — client can request `limit=100_000_000`. | security.md · api.md | `limit: int = Query(50, ge=1, le=100)`. |
| 7 | `app/routers/jobs.py:239-250` | `GET /jobs/{id}/result` refreshes the presigned URL and `db.commit()` — a safe method mutates. | foundations.md (CQS) · api.md ("a GET never mutates") | Move the refresh behind a command, or document the cache-fill exception. |

## Medium

| # | Location | Finding | Convention | Fix |
|---|----------|---------|------------|-----|
| 8 | `app/services/storage.py:82` | `print(f"Delete error: {error}")` in service code. | engineering.md | Use `logger.error(...)` with context. (`app/workers/__main__.py:4` print is a CLI usage message → allowed.) |
| 9 | 13 sites (e.g. `jobs.py:235`, `ws.py:114`) | Naive `datetime.utcnow()`. | foundations.md ("never a naive now()") | Use `datetime.now(UTC)`. |
| 10 | `app/config.py:31-32` | Required secrets (`master_key`, `key_encryption_secret`) default to `""` and boot silently. | engineering.md ("loud on required") | Fail-fast at startup on missing required keys. (Auth already fails closed — good.) |
| 11 | `app/services/dash_session.py`, `app/services/auth.py` | Auth is hand-rolled (master key + SHA-256 API keys + hand-built HMAC dashboard session) rather than IdP-delegated. | security.md | At minimum record a documented deviation; the **dashboard session** is the part most at odds. (SHA-256 on 256-bit random tokens is fine — not passwords.) |
| 12 | `app/main.py:137` | Only `/healthz`; no `/version` endpoint. | engineering.md | Add `/version` (build version + git short SHA from `SYSTEM_VERSION`/`SYSTEM_COMMIT`). |
| 13 | `app/routers/ws.py:123` | WS frames are `{uuid,status,...}` with no `type` discriminator. | api.md | Use `{ type, ...data }`; dispatch on `type`. |

## Low / nits

- `app/deps.py:44,71` — function-local imports (`hash_key`, `dash_session`), likely a circular-import workaround; prefer top-level or restructure the cycle.
- `app/static/dashboard.js` (e.g. `saveUserRow('${u.username}')`) — username interpolated into inline `onclick` without JS-string escaping; `escapeHtml` covers HTML, not a JS string literal. Admin-only surface. Prefer `addEventListener` + `dataset`.
- `app/routers/jobs.py:129` — `(user_row.scalar_one_or_none() or User()).priority or 0` is obscure (throwaway `User()` for a default) and a redundant second user query.
- `fmt` magic strings `("alto","txt","multi")` repeated in `jobs.py`/`poller.py` — hoist to a `Literal`/constant.
- `analytics.py:52,55` / `dashboard.py:475` — f-string SQL interpolation of table/column names and int bounds. Safe today (trusted literals; values are bound params) — keep it strictly literal-only.
- Thin-controller drift: `submit_job` (`jobs.py:43`) holds orchestration (validate → upload → insert → enqueue) rather than delegating to a service. Acceptable at this size.

**Style (auto-enforced):** ruff format (double quotes, width 100) and EditorConfig are present and correct — no manual nits.

## Suggested first commit (mechanical items)

`ruff select` (#2), `check`/`typecheck`/`test` Make targets (#1), `limit` cap (#6),
`print`→logger (#8). These are low-risk and unblock the quality gate.
