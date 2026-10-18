"""Verify that the HTTP target reads the dedicated test database before any test runs.

The sentinel is a minimal completed analysis read back through GET /pix/transaction/{id}.
Pix and analysis rows are immutable by design (database triggers), so the sentinel stays in
the disposable test database, tagged with the `sentinel-` ID prefix and policy `test-sentinel`.
"""

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


SENTINEL_TENANT = "demo"


def insert_sentinel(connection, external_id, analysis_key, marker):
    """A minimal completed analysis: claim, Pix and analysis rows the GET route can read."""
    pix_id = uuid4()
    public_response = {"key": str(analysis_key), "status": "approved", "metadata": {"sentinel": marker}}
    connection.execute(text("""
        INSERT INTO public.analysis_claim
            (tenant_id, external_id, payload_hash, owner_id, fencing_token, lease_until, state, analysis_key)
        VALUES (:tenant, :external_id, :payload_hash, :owner, 1, clock_timestamp(), 'in_progress', NULL)
    """), {"tenant": SENTINEL_TENANT, "external_id": external_id, "payload_hash": uuid4().hex * 2, "owner": uuid4()})
    connection.execute(text("""
        INSERT INTO public.pix_transaction
            (id, tenant_id, external_id, payload_hash, payload, amount_cents, payer_branch, payer_account,
             payer_document, payee_document, participant, destination_branch, destination_account,
             transaction_at, source_opened_at, destination_opened_at, device_id, channel)
        SELECT :pix_id, tenant_id, external_id, payload_hash, '{}'::jsonb, 1, '0001', '1',
               '12345678909', '98765432100', '00000000', '0001', '2',
               now(), now(), now(), 'sentinel', 'test'
        FROM public.analysis_claim WHERE tenant_id = :tenant AND external_id = :external_id
    """), {"pix_id": pix_id, "tenant": SENTINEL_TENANT, "external_id": external_id})
    connection.execute(text("""
        INSERT INTO public.risk_analysis
            (key, tenant_id, pix_id, status, decision_at, policy_version, score, device_state,
             night_qualifies, public_response, snapshot)
        VALUES (:key, :tenant, :pix_id, 'approved', clock_timestamp(), 'test-sentinel', 0, 'unknown',
                false, CAST(:public_response AS jsonb), '{}'::jsonb)
    """), {"key": analysis_key, "tenant": SENTINEL_TENANT, "pix_id": pix_id,
           "public_response": json.dumps(public_response)})


def verify_api_database():
    """Insert a sentinel analysis in the test database and read it back through the API."""
    database_url = require_test_database_url()
    base_url = api_base_url()  # Fail before opening SQL if the target is implicit.
    external_id, analysis_key, marker = f"sentinel-{uuid4().hex}", uuid4(), uuid4().hex
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            insert_sentinel(connection, external_id, analysis_key, marker)
        response = requests.get(
            base_url + "/pix/transaction/" + external_id,
            headers={"INTERNAL-TOKEN": environ.get("INTERNAL_TOKEN", "default_token")},
            timeout=5,
        )
        if (response.status_code != 200 or response.json().get("key") != str(analysis_key)
                or response.json().get("metadata", {}).get("sentinel") != marker):
            raise RuntimeError("API is not reading the configured test database (sentinel mismatch)")
    finally:
        engine.dispose()
