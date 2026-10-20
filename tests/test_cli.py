"""Tests for taas-admin against a throwaway migrated Postgres (``TEST_DATABASE_URL``);
skip otherwise. The CLI opens its own engine from ``DATABASE_URL``."""

import asyncio
import json
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from typer.testing import CliRunner

from app.cli.main import app

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

runner = CliRunner()


async def _truncate(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE backend_domains, domains, job_results, jobs, backends, users, "
                    "config RESTART IDENTITY CASCADE"
                )
            )
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def database(monkeypatch):
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run these tests")
    try:
        asyncio.run(_truncate(TEST_DATABASE_URL))
    except OSError:
        pytest.skip("TEST_DATABASE_URL not reachable")
    monkeypatch.setenv("DATABASE_URL", TEST_DATABASE_URL)
    monkeypatch.setenv("KEY_ENCRYPTION_SECRET", "2bn3JCnUdwtXUSesg0YllMR47VWV4R_DcTt9p57Fy1A=")


def _ok(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return result.output


def test_user_create_prints_the_key_once_and_lists() -> None:
    key = _ok("user", "create", "alice").strip()
    assert len(key) > 20
    users = json.loads(_ok("user", "list", "--json"))
    assert [u["username"] for u in users] == ["alice"]
    assert key not in _ok("user", "list")


def test_creating_a_user_twice_is_refused() -> None:
    _ok("user", "create", "bob")
    result = runner.invoke(app, ["user", "create", "bob"])
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_backend_upsert_is_idempotent(monkeypatch) -> None:
    monkeypatch.setenv("ENGINE_KEY", "s3cret")
    args = ["backend", "upsert", "--url", "http://e1:8000", "--api-key-env", "ENGINE_KEY"]
    _ok(*args, "--max-inflight", "4")
    _ok(*args, "--max-inflight", "6")
    backends = json.loads(_ok("backend", "list", "--json"))
    assert len(backends) == 1
    assert backends[0]["max_inflight"] == 6
    assert "s3cret" not in json.dumps(backends)


def test_bad_backend_url_exits_2() -> None:
    result = runner.invoke(app, ["backend", "upsert", "--url", "ftp://nope"])
    assert result.exit_code == 2
    assert "error: url" in result.output


def test_config_set_then_get() -> None:
    _ok("config", "set", "jobs.max_requeues", "7")
    assert _ok("config", "get", "jobs.max_requeues").strip() == "7"
