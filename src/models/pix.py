"""Explicit SQLAlchemy Core models. SQL migrations, never create_all, own DDL."""
from sqlalchemy import Table, Column, String, CHAR, Text, BigInteger, Integer, Numeric, Boolean, DateTime, LargeBinary
from sqlalchemy.dialects.postgresql import JSONB, UUID
from models.base import Base


def model(name, specs):
    return Table(name, Base.metadata, *(Column(n,t) for n,t in specs))


S=String
U=UUID(as_uuid=True)
T=DateTime(timezone=True)
analysis_claim=model('analysis_claim', [('tenant_id',S(64)),('external_id',S(128)),('payload_hash',CHAR(64)),
    ('owner_id',U),('fencing_token',BigInteger),('lease_until',T),('state',Text),('analysis_key',U)])
source_initial=model('source_initial_check', [('tenant_id',S(64)),('branch',S(4)),('account',S(20)),('document',S(14)),
    ('claim_external_id',S(128)),('owner_id',U),('fencing_token',BigInteger),('lease_until',T),('state',Text),
    ('result',JSONB),('completed_at',T)])
account=model('pix_account',[('tenant_id',S(64)),('branch',S(4)),('account',S(20)),('document',S(14))])
pix=model('pix_transaction', [('id',U),('tenant_id',S(64)),('external_id',S(128)),('payload_hash',CHAR(64)),
    ('payload',JSONB),('raw_body',LargeBinary),('amount_cents',BigInteger),('payer_branch',S(4)),('payer_account',S(20)),
    ('payer_document',S(14)),('payee_document',S(14)),('participant',S(8)),('destination_branch',S(4)),
    ('destination_account',S(20)),('transaction_at',T),('source_opened_at',T),('destination_opened_at',T),
    ('device_id',S(128)),('channel',S(64)),('ip',Text),('geolocation',JSONB),('received_at',T)])
analysis=model('risk_analysis',[('key',U),('tenant_id',S(64)),('pix_id',U),('status',Text),('decision_at',T),
    ('sequence',BigInteger),('policy_version',S(128)),('score',Numeric),('device_state',Text),('night_qualifies',Boolean),
    ('public_response',JSONB),('snapshot',JSONB)])
receipt=model('request_receipt',[('id',U),('request_id',S(128)),('tenant_id',S(64)),('external_id',S(128)),
    ('payload_hash',CHAR(64)),('received_at',T),('http_status',Integer),('category',S(64)),('analysis_key',U)])
challenge=model('bank_challenge',[('tenant_id',S(64)),('provider',S(64)),('challenge_id',S(128)),('pix_id',U),
    ('analysis_key',U),('operation_fingerprint',CHAR(64)),('received_at',T)])
link=model('bank_operation_link',[('tenant_id',S(64)),('provider',S(64)),('operation_id',S(128)),('pix_id',U),
    ('operation_fingerprint',CHAR(64)),('received_at',T)])
event=model('bank_event',[('id',U),('tenant_id',S(64)),('provider',S(64)),('event_id',S(128)),('payload_hash',CHAR(64)),
    ('pix_id',U),('analysis_key',U),('operation_fingerprint',CHAR(64)),('kind',Text),('state',Text),('terminal',Boolean),
    ('challenge_id',S(128)),('occurred_at',T),('received_at',T),('source_version',BigInteger),('payload',JSONB)])
snapshot=model('provider_snapshot',[('id',U),('tenant_id',S(64)),('key_hash',CHAR(64)),('kind',Text),('observed_at',T),
    ('available_at',T),('valid_until',T),('source_version',BigInteger),('fact_hash',CHAR(64)),('record',JSONB),('received_at',T)])
