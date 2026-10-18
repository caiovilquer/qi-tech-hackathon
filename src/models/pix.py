"""SQLAlchemy Core tables for Pix. DDL is owned by the SQL migrations, never by create_all."""
from sqlalchemy import Table, Column, String, CHAR, Text, BigInteger, Integer, Numeric, Boolean, DateTime, LargeBinary
from sqlalchemy.dialects.postgresql import JSONB, UUID
from models.base import Base

UUID_COLUMN = UUID(as_uuid=True)
TIMESTAMP = DateTime(timezone=True)


def table(name, columns):
    return Table(name, Base.metadata, *(Column(column_name, column_type) for column_name, column_type in columns))


analysis_claim = table('analysis_claim', [
    ('tenant_id', String(64)), ('external_id', String(128)), ('payload_hash', CHAR(64)), ('owner_id', UUID_COLUMN),
    ('fencing_token', BigInteger), ('lease_until', TIMESTAMP), ('state', Text), ('analysis_key', UUID_COLUMN)])
source_initial = table('source_initial_check', [
    ('tenant_id', String(64)), ('branch', String(4)), ('account', String(20)), ('document', String(14)),
    ('claim_external_id', String(128)), ('owner_id', UUID_COLUMN), ('fencing_token', BigInteger),
    ('lease_until', TIMESTAMP), ('state', Text), ('result', JSONB), ('completed_at', TIMESTAMP)])
account = table('pix_account', [
    ('tenant_id', String(64)), ('branch', String(4)), ('account', String(20)), ('document', String(14))])
pix = table('pix_transaction', [
    ('id', UUID_COLUMN), ('tenant_id', String(64)), ('external_id', String(128)), ('payload_hash', CHAR(64)),
    ('payload', JSONB), ('raw_body', LargeBinary), ('amount_cents', BigInteger), ('payer_branch', String(4)),
    ('payer_account', String(20)), ('payer_document', String(14)), ('payee_document', String(14)),
    ('participant', String(8)), ('destination_branch', String(4)), ('destination_account', String(20)),
    ('transaction_at', TIMESTAMP), ('source_opened_at', TIMESTAMP), ('destination_opened_at', TIMESTAMP),
    ('device_id', String(128)), ('channel', String(64)), ('ip', Text), ('geolocation', JSONB), ('received_at', TIMESTAMP)])
analysis = table('risk_analysis', [
    ('key', UUID_COLUMN), ('tenant_id', String(64)), ('pix_id', UUID_COLUMN), ('status', Text), ('decision_at', TIMESTAMP),
    ('sequence', BigInteger), ('policy_version', String(128)), ('score', Numeric), ('device_state', Text),
    ('night_qualifies', Boolean), ('public_response', JSONB), ('snapshot', JSONB)])
receipt = table('request_receipt', [
    ('id', UUID_COLUMN), ('request_id', String(128)), ('tenant_id', String(64)), ('external_id', String(128)),
    ('payload_hash', CHAR(64)), ('received_at', TIMESTAMP), ('http_status', Integer), ('category', String(64)),
    ('analysis_key', UUID_COLUMN)])
challenge = table('bank_challenge', [
    ('tenant_id', String(64)), ('provider', String(64)), ('challenge_id', String(128)), ('pix_id', UUID_COLUMN),
    ('analysis_key', UUID_COLUMN), ('operation_fingerprint', CHAR(64)), ('received_at', TIMESTAMP)])
link = table('bank_operation_link', [
    ('tenant_id', String(64)), ('provider', String(64)), ('operation_id', String(128)), ('pix_id', UUID_COLUMN),
    ('operation_fingerprint', CHAR(64)), ('received_at', TIMESTAMP)])
event = table('bank_event', [
    ('id', UUID_COLUMN), ('tenant_id', String(64)), ('provider', String(64)), ('event_id', String(128)),
    ('payload_hash', CHAR(64)), ('pix_id', UUID_COLUMN), ('analysis_key', UUID_COLUMN), ('operation_fingerprint', CHAR(64)),
    ('kind', Text), ('state', Text), ('terminal', Boolean), ('challenge_id', String(128)), ('occurred_at', TIMESTAMP),
    ('received_at', TIMESTAMP), ('source_version', BigInteger), ('payload', JSONB)])
snapshot = table('provider_snapshot', [
    ('id', UUID_COLUMN), ('tenant_id', String(64)), ('key_hash', CHAR(64)), ('kind', Text), ('observed_at', TIMESTAMP),
    ('available_at', TIMESTAMP), ('valid_until', TIMESTAMP), ('source_version', BigInteger), ('fact_hash', CHAR(64)),
    ('record', JSONB), ('received_at', TIMESTAMP)])
