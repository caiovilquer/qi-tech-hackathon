"""Regression evidence for the re-review findings on PR #6 (second round)."""
from dataclasses import replace
from time import monotonic

from sqlalchemy import text

from tests.integration.test_pix_api import api, controller, payload, post, IDENTITY
from tests.integration.test_pix_storage import storage, save, event
from tests.integration.test_migrations import migration_db
from antifraud.context import BankPayment, ExecutionEvent
from providers.fixtures import FixtureBankProvider
from utils.pix_storage_codec import fingerprint


def linked_history(storage, external, operation, execution):
    old=payload(external);old['transaction_date']='2026-10-14T12:00:00Z'
    _,p,dto=save(storage,payload=old)
    storage.link_bank_operation('demo',IDENTITY.name,operation,p.id,fingerprint(p))
    payment=BankPayment(IDENTITY.name,operation,replace(p,id=operation),
        ExecutionEvent(execution,p.payer,operation,'executed',p.transaction_at,p.transaction_at))
    return p,dto,FixtureBankProvider(IDENTITY,history=(payment,))


def test_local_terminal_execution_contradicted_by_bank_history_is_409_not_retry_loop(api,storage):
    # Local fact: the linked operation was ingested as a terminal failure. The bank
    # history reports the same execution as executed. Only the institution can repair it.
    p,dto,bank=linked_history(storage,'terminal-conflict','op-terminal','exec-terminal')
    assert storage.ingest(event(p,dto,'failed',occurred=p.transaction_at,event_id='exec-terminal'))=='created'
    url=api(controller(storage,bank=bank));body=payload()
    for _ in range(2):  # same ID twice: no lease wait, no takeover loop, no retry instruction
        start=monotonic();r=post(url,body)
        assert r.status_code==409 and r.json()['code']=='QIT002010',r.text
        assert 'reconcil' in r.json()['description'].lower() and monotonic()-start<1
    other=post(url,payload());assert other.status_code==409 and other.json()['code']=='QIT002010',other.text
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==1
        assert repo.session.execute(text("SELECT count(*) FROM request_receipt WHERE http_status=409 AND category='QIT002010'")).scalar()==3
        assert repo.session.execute(text("SELECT bool_and(lease_until<=clock_timestamp()) FROM analysis_claim WHERE state='in_progress'")).scalar()
        assert repo.session.execute(text("SELECT count(*) FROM analysis_claim WHERE state='in_progress'")).scalar()==2


def test_conflicting_execution_identity_is_409_at_both_detection_points(api,storage,monkeypatch):
    # The same durable contradiction must not be 409 when it lands between enrichment
    # and _persist but 503 when it is already present at the first read.
    p,dto,bank=linked_history(storage,'identity-conflict','op-identity','exec-identity')
    service=controller(storage,bank=bank);original=service._persist
    def contradicted(enriched):
        storage.ingest(event(p,dto,'failed',occurred=p.transaction_at,event_id='exec-identity'))
        return original(enriched)
    monkeypatch.setattr(service,'_persist',contradicted)
    url=api(service)
    first=post(url,payload());assert first.status_code==409 and first.json()['code']=='QIT002010',first.text
    monkeypatch.setattr(service,'_persist',original)
    second=post(url,payload());assert second.status_code==409 and second.json()['code']=='QIT002010',second.text
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM bank_event')).scalar()==1
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==1
