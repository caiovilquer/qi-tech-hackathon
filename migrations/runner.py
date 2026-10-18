"""Small SQL migration runner; no application models or startup side effects.

One transaction and one database-wide advisory lock protect DDL and history.
Applied SQL is immutable (SHA-256). No automatic downgrade or schema repair.
"""

from hashlib import sha256
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import inspect, text


DIRECTORY = Path(__file__).parent
LOCK_ID = 73412690586274901
BASE_TABLES = ("sample_entity_status", "sample_entity", "sample_entity_status_event")


class MigrationError(RuntimeError):
    """Migration history or legacy schema does not match this checkout."""


def _signature(connection, schema):
    inspector = inspect(connection)

    def expression(value):
        if value is None:
            return None
        return value.replace(f'"{schema}".', "").replace(f"{schema}.", "")

    result = {}
    for table in BASE_TABLES:
        columns = [
            (c["name"], str(c["type"]), c["nullable"], expression(c["default"]),
             c.get("identity"), c.get("computed"))
            for c in inspector.get_columns(table, schema=schema)
        ]
        primary = inspector.get_pk_constraint(table, schema=schema)["constrained_columns"]
        unique = sorted(tuple(c["column_names"]) for c in
                        inspector.get_unique_constraints(table, schema=schema))
        foreign = sorted((tuple(c["constrained_columns"]),
                          "self" if c["referred_schema"] in (None, schema) else c["referred_schema"],
                          c["referred_table"], tuple(c["referred_columns"]),
                          tuple(sorted(c["options"].items())))
                         for c in inspector.get_foreign_keys(table, schema=schema))
        checks = sorted(expression(c["sqltext"]) for c in
                        inspector.get_check_constraints(table, schema=schema))
        parameters = {"schema": schema, "table": table}
        collations = connection.execute(text("""
            SELECT a.attnum, a.attcollation
            FROM pg_attribute a JOIN pg_class t ON t.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = :schema AND t.relname = :table
                AND a.attnum > 0 AND NOT a.attisdropped ORDER BY a.attnum
        """), parameters).all()
        indexes = connection.execute(text("""
            SELECT am.amname, i.indisprimary, i.indisvalid, i.indisready,
                i.indimmediate, i.indnullsnotdistinct, i.indnkeyatts,
                i.indkey::text, i.indclass::text, i.indcollation::text,
                i.indoption::text, pg_get_expr(i.indexprs, i.indrelid),
                pg_get_expr(i.indpred, i.indrelid)
            FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN pg_class ix ON ix.oid = i.indexrelid
            JOIN pg_am am ON am.oid = ix.relam
            WHERE n.nspname = :schema AND t.relname = :table AND i.indisunique
        """), parameters).all()
        constraints = connection.execute(text("""
            SELECT c.contype, c.convalidated, c.condeferrable, c.condeferred,
                c.connoinherit, pg_get_constraintdef(c.oid)
            FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = :schema AND t.relname = :table
        """), parameters).all()
        security = connection.execute(text("""
            SELECT t.relrowsecurity, t.relforcerowsecurity,
                EXISTS (SELECT 1 FROM pg_trigger g
                        WHERE g.tgrelid = t.oid AND NOT g.tgisinternal)
            FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = :schema AND t.relname = :table
        """), parameters).one()
        sequence = connection.execute(text("""
            SELECT s.seqtypid, s.seqstart, s.seqincrement, s.seqmax,
                s.seqmin, s.seqcache, s.seqcycle
            FROM pg_sequence s JOIN pg_class t ON t.oid = s.seqrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = :schema AND t.relname = :table || '_id_seq'
        """), parameters).all()

        def catalog(rows):
            return sorted((tuple(expression(v) if isinstance(v, str) else v for v in row)
                           for row in rows), key=repr)

        result[table] = (columns, primary, unique, foreign, checks,
                         catalog(collations), catalog(indexes), catalog(constraints),
                         tuple(security), catalog(sequence))
    return result


def _adopt_base(connection, sql):
    """Compare catalogs to the actual initial DDL in an isolated reference schema.

    All reference DDL is rolled back on failure, and removed on success.
    Names of constraints can differ; definitions, column order and defaults cannot.
    """
    existing = set(inspect(connection).get_table_names(schema="public"))
    if not existing.intersection(BASE_TABLES):
        return False
    if not set(BASE_TABLES).issubset(existing):
        raise MigrationError("Partial Base API schema; refusing adoption")
    reference = "migration_reference_" + uuid4().hex
    connection.exec_driver_sql(f'CREATE SCHEMA "{reference}"')
    connection.exec_driver_sql(f'SET LOCAL search_path TO "{reference}"')
    connection.exec_driver_sql(sql, execution_options={"no_parameters": True})
    expected = _signature(connection, reference)
    connection.exec_driver_sql("SET LOCAL search_path TO public")
    if _signature(connection, "public") != expected:
        raise MigrationError("Base API schema differs from initial DDL; refusing adoption")
    statuses = connection.exec_driver_sql(
        "SELECT enumerator FROM public.sample_entity_status ORDER BY enumerator"
    ).scalars().all()
    if statuses != ["created", "failed", "pending", "success"]:
        raise MigrationError("Base API status seed differs; refusing adoption")
    for table in BASE_TABLES:
        # Names are fixed by the initial DDL and signature checked above.
        maximum = connection.exec_driver_sql(f'SELECT max(id) FROM public."{table}"').scalar_one()
        last_value, called = connection.exec_driver_sql(
            f'SELECT last_value, is_called FROM public."{table}_id_seq"'
        ).one()
        if maximum is not None and last_value + int(called) <= maximum:
            raise MigrationError("Base API sequence is behind existing IDs; refusing adoption")
    connection.exec_driver_sql(f'DROP SCHEMA "{reference}" CASCADE')
    return True


def upgrade(engine, directory=DIRECTORY, *, adopt_base=False):
    if engine.dialect.name != "postgresql":
        raise MigrationError("PostgreSQL is required")
    files = sorted(Path(directory).glob("*.sql"))
    if not files or files[0].name != "0001_base.sql":
        raise MigrationError("Missing initial migration")
    migrations = []
    for number, path in enumerate(files, 1):
        if not re.fullmatch(rf"{number:04d}_[a-z0-9_]+\.sql", path.name):
            raise MigrationError("Migrations must be sequential: 0001_name.sql, 0002_name.sql")
        raw = path.read_bytes()
        migrations.append((path.name, sha256(raw).hexdigest(), raw.decode("utf-8")))

    applied = []
    with engine.execution_options(isolation_level="READ COMMITTED").begin() as connection:
        connection.exec_driver_sql("SET LOCAL search_path TO public")
        connection.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
        connection.exec_driver_sql("SET LOCAL statement_timeout TO '30s'")
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LOCK_ID})
        connection.exec_driver_sql("""
            CREATE TABLE IF NOT EXISTS public.schema_migration (
                version TEXT PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        """)
        history = connection.exec_driver_sql(
            "SELECT version, checksum FROM public.schema_migration ORDER BY version"
        ).all()
        expected = [(name, checksum) for name, checksum, _ in migrations]
        if [tuple(row) for row in history] != expected[:len(history)] or len(history) > len(expected):
            raise MigrationError("Unknown, out-of-order or modified applied migration")
        for name, checksum, sql in migrations[len(history):]:
            adopted = name == "0001_base.sql" and adopt_base and _adopt_base(connection, sql)
            if not adopted:
                connection.exec_driver_sql(sql, execution_options={"no_parameters": True})
            connection.execute(text(
                "INSERT INTO public.schema_migration (version, checksum) VALUES (:version, :checksum)"
            ), {"version": name, "checksum": checksum})
            applied.append(name)
    return applied
