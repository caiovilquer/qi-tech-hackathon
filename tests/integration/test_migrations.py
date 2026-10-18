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
            connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        admin.dispose()


def migration_files(tmp_path, extra=None):
    initial = Path(DIRECTORY, "0001_base.sql").read_bytes()
    (tmp_path / "0001_base.sql").write_bytes(initial)
    if extra:
        (tmp_path / "0002_next.sql").write_text(extra)
    return tmp_path


@pytest.mark.parametrize("adopt_base", [False, True])
def test_empty_database_and_second_run(migration_db, adopt_base):
    assert upgrade(migration_db, adopt_base=adopt_base) == ["0001_base.sql"]
    assert upgrade(migration_db, adopt_base=adopt_base) == []
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
    "CREATE UNIQUE INDEX extra_name_unique ON sample_entity (name)",
    "CREATE UNIQUE INDEX extra_partial_unique ON sample_entity (name) WHERE counter > 0",
    "CREATE UNIQUE INDEX extra_expression_unique ON sample_entity (lower(name))",
    'ALTER TABLE sample_entity ALTER COLUMN email TYPE VARCHAR(255) COLLATE "C"',
    """ALTER TABLE sample_entity DROP CONSTRAINT sample_entity_status_id_fkey;
       ALTER TABLE sample_entity ADD FOREIGN KEY (status_id)
       REFERENCES sample_entity_status(id) NOT VALID""",
    "SELECT setval('sample_entity_id_seq', 1, false)",
    "ALTER SEQUENCE sample_entity_id_seq INCREMENT BY 2",
    "ALTER TABLE sample_entity ENABLE ROW LEVEL SECURITY",
    """CREATE FUNCTION legacy_trigger() RETURNS trigger LANGUAGE plpgsql AS
       $$ BEGIN RETURN NEW; END $$;
       CREATE TRIGGER legacy_trigger BEFORE INSERT ON sample_entity
       FOR EACH ROW EXECUTE FUNCTION legacy_trigger()""",
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


@pytest.mark.parametrize("default_isolation", ["read committed", "repeatable read", "serializable"])
@pytest.mark.parametrize("preexisting", [False, True])
def test_concurrent_migrators_apply_exactly_once(migration_db, tmp_path, default_isolation, preexisting):
    if preexisting:
        upgrade(migration_db)
        with migration_db.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE once_only (isolation TEXT)")
    # The database setting applies to NEW connections, as on a configured server.
    with migration_db.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql(
            f'ALTER DATABASE "{migration_db.url.database}" '
            f"SET default_transaction_isolation TO '{default_isolation}'"
        )
    migration_db.dispose()
    script = """
        SELECT pg_sleep(0.2);
        INSERT INTO once_only VALUES (current_setting('transaction_isolation'));
    """
    if not preexisting:
        script = "CREATE TABLE once_only (isolation TEXT);" + script
    directory = migration_files(tmp_path, script)
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return upgrade(migration_db, directory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sorted(map(len, results)) == [0, 1 if preexisting else 2]
    with migration_db.connect() as connection:
        assert connection.exec_driver_sql("SELECT isolation FROM once_only").scalars().all() == ["read committed"]
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


@pytest.mark.parametrize("expression, expected", [
    ("'100%'", "100%"),
    ("'100%%'", "100%%"),
    ("format('%s', 'x')", "x"),
])
def test_migration_preserves_percent_literals(migration_db, tmp_path, expression, expected):
    directory = migration_files(tmp_path, f"""
        CREATE TABLE percent_literal (value TEXT);
        INSERT INTO percent_literal VALUES ({expression});
    """)
    upgrade(migration_db, directory)
    with migration_db.connect() as connection:
        assert connection.exec_driver_sql("SELECT value FROM percent_literal").scalar_one() == expected


def test_migration_with_like_and_plpgsql_percent(migration_db, tmp_path):
    directory = migration_files(tmp_path, """
        CREATE TABLE percent_rule (value TEXT CHECK (value LIKE 'a%'));
        CREATE FUNCTION percent_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.value = 'alarm' THEN
                RAISE EXCEPTION 'invalid value %', NEW.value;
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER percent_guard BEFORE INSERT ON percent_rule
            FOR EACH ROW EXECUTE FUNCTION percent_guard();
        INSERT INTO percent_rule VALUES ('allowed');
    """)
    upgrade(migration_db, directory)
    with migration_db.connect() as connection:
        assert connection.exec_driver_sql("SELECT value FROM percent_rule").scalar_one() == "allowed"
    with pytest.raises(DBAPIError, match="invalid value alarm"):
        with migration_db.begin() as connection:
            connection.exec_driver_sql("INSERT INTO percent_rule VALUES ('alarm')")


def test_adoption_executes_initial_ddl_with_literal_percent(migration_db, tmp_path):
    seed_legacy(migration_db)
    directory = migration_files(tmp_path)
    with (directory / "0001_base.sql").open("a") as output:
        output.write("\n-- original bytes include a literal 100% and 100%%\n")
    assert upgrade(migration_db, directory, adopt_base=True) == ["0001_base.sql"]


def test_api_uses_public_despite_user_schema(migration_db):
    import os
    import subprocess
    import sys

    with migration_db.begin() as connection:
        connection.exec_driver_sql('CREATE SCHEMA "bootcamp"')
        connection.exec_driver_sql('CREATE TABLE "bootcamp".sample_entity (wrong INTEGER)')
    environment = dict(os.environ, DATABASE_URL=migration_db.url.render_as_string(hide_password=False),
                       PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    result = subprocess.run([sys.executable, "-c", """
from database import engine
with engine.connect() as c:
    assert c.exec_driver_sql('SHOW search_path').scalar_one() == 'public'
    assert c.exec_driver_sql('SELECT current_schema()').scalar_one() == 'public'
engine.dispose()
"""], env=environment, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
