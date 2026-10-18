import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from migrations.runner import upgrade
from tests.integration.test_migrations import migration_db
from tests.utils.db_utils import DbUtils
from tests.utils.environment import verify_api_database
from tests.utils.requisition import ClientRequisition


def row_count(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql("SELECT count(*) FROM public.pix_transaction").scalar_one()


def sentinel_count(engine):
    with engine.connect() as connection:
        return connection.exec_driver_sql(
            "SELECT count(*) FROM public.risk_analysis WHERE policy_version = 'test-sentinel'").scalar_one()


def test_matching_http_target_is_verified_with_one_tagged_sentinel():
    engine = create_engine(DbUtils.database_url())
    try:
        before, tagged = row_count(engine), sentinel_count(engine)
        verify_api_database()
        # Pix rows are immutable, so the sentinel stays; it is recognizable and never a client Pix.
        assert row_count(engine) == before + 1
        assert sentinel_count(engine) == tagged + 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("missing", ["SERVER_LOCALHOST", "API_PORT"])
def test_http_helpers_refuse_implicit_target(monkeypatch, missing):
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match="explicit SERVER_LOCALHOST and API_PORT"):
        ClientRequisition.send("POST", "/pix/transaction", payload={})
    with pytest.raises(RuntimeError, match="explicit SERVER_LOCALHOST and API_PORT"):
        verify_api_database()


def test_auth_failure_is_reported_as_mismatch(monkeypatch):
    engine = create_engine(DbUtils.database_url())
    try:
        before = row_count(engine)
        monkeypatch.setenv("INTERNAL_TOKEN", "wrong-test-token")
        with pytest.raises(RuntimeError, match="sentinel mismatch"):
            verify_api_database()
        assert row_count(engine) == before + 1
    finally:
        engine.dispose()


def test_pytest_aborts_when_api_reads_another_database(migration_db):
    upgrade(migration_db)
    api_database = create_engine(DbUtils.database_url())
    try:
        before = row_count(api_database)
        environment = dict(os.environ, DATABASE_URL=migration_db.url.render_as_string(hide_password=False))
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "tests/integration/test_healthcheck.py"],
            cwd=Path(__file__).resolve().parents[2], env=environment,
            capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 4, result.stdout + result.stderr
        assert "sentinel mismatch" in result.stderr
        assert "no tests were run" in result.stderr
        assert row_count(api_database) == before  # the API never saw the other database's sentinel
        assert row_count(migration_db) == 1
    finally:
        api_database.dispose()
