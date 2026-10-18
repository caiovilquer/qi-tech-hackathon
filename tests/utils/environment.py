"""Verify the HTTP target reads the dedicated database before running tests."""

import json
from os import environ
from uuid import uuid4

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


def require_test_database_url():
    value = environ.get("DATABASE_URL")
    if (environ.get("APP_ENV") != "testing" or not value
            or make_url(value).get_backend_name() != "postgresql"
            or not (make_url(value).database or "").startswith("pix_test_")):
        raise RuntimeError("Refusing reset/tests outside a dedicated pix_test_ PostgreSQL database")
    return value


def api_base_url():
    host, port = environ.get("SERVER_LOCALHOST"), environ.get("API_PORT")
    if environ.get("APP_ENV") == "testing" and (not host or not port):
        raise RuntimeError("Testing requires explicit SERVER_LOCALHOST and API_PORT")
    host, port = host or "127.0.0.1", port or "3000"
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise RuntimeError("API_PORT must be an integer between 1 and 65535")
    return f"http://{host}:{port}"


def verify_api_database():
    database_url = require_test_database_url()
    base_url = api_base_url()  # Fail before opening SQL if target is implicit.
    key, marker = str(uuid4()), uuid4().hex
    engine = create_engine(database_url)
    inserted = False
    try:
        with engine.begin() as connection:
            connection.execute(text("""
                INSERT INTO public.sample_entity
                    (sample_entity_key, status_id, sample_entity_data, name,
                     email, document_number, birthdate, counter)
                VALUES (:key, (SELECT id FROM public.sample_entity_status
                              WHERE enumerator = 'pending'),
                        CAST(:data AS jsonb), 'Test database identity', :email,
                        :document, '1990-01-01', 0)
            """), {"key": key, "data": json.dumps({"hello": marker}),
                   "email": marker + "@example.test",
                   "document": f"{uuid4().int % 10**14:014d}"})
        inserted = True
        response = requests.get(
            base_url + "/sample_entity/" + key,
            headers={"INTERNAL-TOKEN": environ.get("INTERNAL_TOKEN", "default_token")},
            timeout=5,
        )
        if (response.status_code != 200 or response.json().get("sample_entity_key") != key
                or response.json().get("hello") != marker):
            raise RuntimeError("API is not reading the configured test database (sentinel mismatch)")
    finally:
        try:
            if inserted:
                with engine.begin() as connection:
                    connection.execute(text(
                        "DELETE FROM public.sample_entity WHERE sample_entity_key = :key"
                    ), {"key": key})
        finally:
            engine.dispose()
