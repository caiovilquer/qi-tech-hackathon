from sqlalchemy import create_engine, text

from tests.utils import DbUtils, PayloadGenerator, RequestGenerator


def test_http_write_is_committed_in_postgres_and_readable():
    payload = PayloadGenerator.create_sample_entity_payload()
    status, created = RequestGenerator.POST_sample_entity(payload)
    assert status == 201
    key = created["sample_entity_key"]
    engine = create_engine(DbUtils.database_url())
    try:
        with engine.connect() as connection:
            row = connection.execute(text("""
                SELECT name, email, sample_entity_data, counter
                FROM sample_entity WHERE sample_entity_key = :key
            """), {"key": key}).one()
            assert (row.name, row.email, row.counter) == (payload["name"], payload["email"], 0)
            assert row.sample_entity_data["hello"] == payload["hello"]
    finally:
        engine.dispose()
    status, fetched = RequestGenerator.GET_sample_entity(key)
    assert status == 200
    assert fetched["email"] == payload["email"]
    assert fetched["sample_entity_key"] == key
