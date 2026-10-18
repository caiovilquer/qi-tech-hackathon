from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from migrations.runner import DIRECTORY, MigrationError, upgrade
from tests.utils.db_utils import DbUtils


@pytest.fixture
def migration_db():
    """Every test owns a freshly created database in the isolated test server."""
    url = make_url(DbUtils.database_url())
    database = "pix_test_migration_" + uuid4().hex
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    engine = create_engine(url.set(database=database))
    with admin.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{database}"')
        admin.dispose()


def migration_files(tmp_path, extra=None):
    initial = Path(DIRECTORY, "0001_base.sql").read_bytes()
    (tmp_path / "0001_base.sql").write_bytes(initial)
    if extra:
        (tmp_path / "0002_next.sql").write_text(extra)
    return tmp_path


def test_empty_database_and_second_run(migration_db):
    assert upgrade(migration_db) == ["0001_base.sql"]
    assert upgrade(migration_db) == []
    with migration_db.connect() as connection:
        assert set(inspect(connection).get_table_names()) == {
            "schema_migration", "sample_entity", "sample_entity_status", "sample_entity_status_event"
        }
        assert connection.exec_driver_sql(
            "SELECT enumerator FROM sample_entity_status ORDER BY enumerator"
        ).scalars().all() == ["created", "failed", "pending", "success"]


def test_existing_versioned_database_upgrade_preserves_data(migration_db, tmp_path):
    upgrade(migration_db)
    with migration_db.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE preserved (value INTEGER)")
        connection.exec_driver_sql("INSERT INTO preserved VALUES (42)")
    directory = migration_files(tmp_path, "ALTER TABLE preserved ADD COLUMN label TEXT DEFAULT 'kept';")
    assert upgrade(migration_db, directory) == ["0002_next.sql"]
    with migration_db.connect() as connection:
        assert connection.exec_driver_sql("SELECT value, label FROM preserved").one() == (42, "kept")


def seed_legacy(engine):
    with engine.begin() as connection:
        connection.exec_driver_sql(Path(DIRECTORY, "0001_base.sql").read_text())
        connection.exec_driver_sql("""
            INSERT INTO sample_entity
                (sample_entity_key, status_id, sample_entity_data, name, email,
                 document_number, birthdate, counter)
            VALUES ('00000000-0000-0000-0000-000000000001',
                    (SELECT id FROM sample_entity_status WHERE enumerator='pending'),
                    '{"hello":"legacy"}', 'Synthetic', 'legacy@example.test',
                    '12345678909', '1990-01-01', 7)
        """)


def test_explicit_legacy_adoption_preserves_rows_and_sequence(migration_db):
    seed_legacy(migration_db)
    assert upgrade(migration_db, adopt_base=True) == ["0001_base.sql"]
    assert upgrade(migration_db, adopt_base=True) == []
    with migration_db.connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT name, counter, sample_entity_data FROM sample_entity"
        ).one() == ("Synthetic", 7, {"hello": "legacy"})
        assert connection.exec_driver_sql("SELECT nextval('sample_entity_id_seq')").scalar_one() == 2
        assert not any(s.startswith("migration_reference_") for s in inspect(connection).get_schema_names())


def test_legacy_requires_explicit_adoption(migration_db):
    seed_legacy(migration_db)
    with pytest.raises(DBAPIError):
        upgrade(migration_db)
    with migration_db.connect() as connection:
        assert not inspect(connection).has_table("schema_migration")
        assert connection.exec_driver_sql("SELECT count(*) FROM sample_entity").scalar_one() == 1


@pytest.mark.parametrize("drift", [
    "ALTER TABLE sample_entity ALTER COLUMN name DROP NOT NULL",
    "ALTER TABLE sample_entity ALTER COLUMN counter TYPE BIGINT",
    "ALTER TABLE sample_entity ALTER COLUMN created_at DROP DEFAULT",
    "ALTER TABLE sample_entity DROP CONSTRAINT sample_entity_status_id_fkey",
    "ALTER TABLE sample_entity DROP CONSTRAINT sample_entity_email_key",
    "DELETE FROM sample_entity_status WHERE enumerator='created'",
    "DROP TABLE sample_entity_status_event",
])
def test_legacy_drift_is_rejected_without_modification(migration_db, drift):
    seed_legacy(migration_db)
    with migration_db.begin() as connection:
        connection.exec_driver_sql(drift)
    with pytest.raises(MigrationError):
        upgrade(migration_db, adopt_base=True)
    with migration_db.connect() as connection:
        assert not inspect(connection).has_table("schema_migration")
        assert connection.exec_driver_sql("SELECT count(*) FROM sample_entity").scalar_one() == 1
        assert not any(s.startswith("migration_reference_") for s in inspect(connection).get_schema_names())


def test_failed_ddl_rolls_back_schema_and_history(migration_db, tmp_path):
    directory = migration_files(tmp_path, "CREATE TABLE unfinished (id INTEGER); SELECT 1 / 0;")
    with pytest.raises(DBAPIError):
        upgrade(migration_db, directory)
    with migration_db.connect() as connection:
        assert inspect(connection).get_table_names() == []


def test_failed_incremental_upgrade_keeps_existing_version(migration_db, tmp_path):
    upgrade(migration_db)
    directory = migration_files(tmp_path, "CREATE TABLE unfinished (id INTEGER); SELECT 1 / 0;")
    with pytest.raises(DBAPIError):
        upgrade(migration_db, directory)
    with migration_db.connect() as connection:
        assert not inspect(connection).has_table("unfinished")
        assert connection.exec_driver_sql("SELECT version FROM schema_migration").scalars().all() == ["0001_base.sql"]


def test_applied_sql_cannot_be_changed(migration_db, tmp_path):
    upgrade(migration_db)
    directory = migration_files(tmp_path)
    with (directory / "0001_base.sql").open("a") as output:
        output.write("\n-- changed\n")
    with pytest.raises(MigrationError, match="modified"):
        upgrade(migration_db, directory)


def test_unknown_applied_version_is_rejected(migration_db):
    upgrade(migration_db)
    with migration_db.begin() as connection:
        connection.execute(text("INSERT INTO schema_migration VALUES (:v, :h, now())"),
                           {"v": "0002_unknown.sql", "h": "a" * 64})
    with pytest.raises(MigrationError, match="Unknown"):
        upgrade(migration_db)


def test_concurrent_migrators_apply_exactly_once(migration_db, tmp_path):
    directory = migration_files(tmp_path, """
        SELECT pg_sleep(0.2);
        CREATE TABLE once_only (id INTEGER);
        INSERT INTO once_only VALUES (1);
    """)
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return upgrade(migration_db, directory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(map(len, results)) == [0, 2]
    with migration_db.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM once_only").scalar_one() == 1
        assert connection.exec_driver_sql("SELECT count(*) FROM schema_migration").scalar_one() == 2


@pytest.mark.parametrize("filename", ["0003_gap.sql", "0001_duplicate.sql", "invalid.sql"])
def test_invalid_migration_sequence_is_rejected(migration_db, tmp_path, filename):
    directory = migration_files(tmp_path)
    (directory / filename).write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        upgrade(migration_db, directory)


def test_reset_refuses_application_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://bootcamp:fake@db/bootcamp")
    with pytest.raises(RuntimeError, match="Refusing reset"):
        DbUtils.rollback()


def test_reset_requires_testing_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    with pytest.raises(RuntimeError, match="Refusing reset"):
        DbUtils.rollback()
