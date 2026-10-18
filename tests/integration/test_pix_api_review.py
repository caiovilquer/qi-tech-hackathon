"""Regression evidence for each independent review finding on PR #6."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace
from uuid import uuid4

import pytest
import requests
from sqlalchemy import text

from tests.integration.test_pix_api import api, controller, payload, post, get, IDENTITY
from tests.integration.test_pix_storage import storage, save, normalized, event
from tests.integration.test_migrations import migration_db
from antifraud.adapter import to_pix
from antifraud.context import BankPayment, ExecutionEvent
from providers.fixtures import FixtureBankProvider
from utils.pix_storage_codec import fingerprint


def test_ownership_conflict_is_actionable_and_preserves_hash_without_busy_loop(api,storage):
    url=api();assert post(url,payload()).status_code==201
    p=payload();p['source_account']['owner']['document_number']='98765432100'
    for _ in range(2):
        r=post(url,p)
        assert r.status_code==409 and r.json()['code']=='QIT002010',r.text
        assert 'reconcil' in r.json()['description'].lower()
        assert 'reconcil' in r.json()['translation'].lower()
    changed=deepcopy(p);changed['amount']+=1
    assert post(url,changed).json()['code']=='QIT002002'
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==1
        row=repo.session.execute(text('SELECT payload_hash,lease_until<=clock_timestamp() AS released FROM analysis_claim WHERE external_id=:id'),{'id':p['id']}).one()
        assert row.payload_hash==normalized(payload=p).payload_hash and row.released


def test_missing_bank_link_is_actionable_and_can_be_reconciled_without_wait(api,storage):
    old=payload('overlap');old['transaction_date']='2026-10-14T12:00:00Z'
    _,p,dto=save(storage,payload=old)
    payment=BankPayment(IDENTITY.name,p.id,p,ExecutionEvent('exec-overlap',p.payer,p.id,'executed',p.transaction_at,p.transaction_at))
    service=controller(storage,bank=FixtureBankProvider(IDENTITY,history=(payment,)))
    url=api(service);body=payload()
    for _ in range(2):
        r=post(url,body);assert r.status_code==409 and r.json()['code']=='QIT002010',r.text
    storage.link_bank_operation('demo',IDENTITY.name,p.id,p.id,fingerprint(p))
    r=post(url,body);assert r.status_code==201,r.text
    assert post(url,body).status_code==200


def test_unauthenticated_flood_never_touches_storage(api,storage,monkeypatch):
    service=controller(storage);calls=[]
    def forbidden(*args,**kwargs):
        calls.append(1)
        raise AssertionError('Authentication failure must not acquire a DB connection')
    monkeypatch.setattr(storage,'receive',forbidden)
    url=api(service)
    def anonymous(i):
        if i%2:return get(url,'id','bad-token')
        return post(url,{},'bad-token')
    with ThreadPoolExecutor(max_workers=20) as pool:
        rs=list(pool.map(anonymous,range(40)))
    assert all(r.status_code==403 for r in rs)
    assert not calls


@pytest.mark.parametrize('seconds',[-86400,1,86400])
def test_local_wall_clock_skew_does_not_break_initial_coverage(api,storage,monkeypatch,seconds):
    import controllers.pix_api as module
    skew=lambda:datetime.now(timezone.utc)+timedelta(seconds=seconds)
    # Patch the controller clock used by the old coordinator. The production fix
    # intentionally removes reliance on it; raising=False keeps that regression.
    monkeypatch.setattr(module,'utc_now',skew,raising=False)
    enricher=module.Enricher
    def default_wall_clock(*a,**kw):
        kw.setdefault('clock',skew)
        return enricher(*a,**kw)
    monkeypatch.setattr(module,'Enricher',default_wall_clock)
    r=post(api(),body:=payload())
    assert r.status_code==201,r.text
    with storage.read() as repo:
        result=repo.initial(to_pix(normalized(payload=body),'demo').payer)
        assert repo.now()-timedelta(seconds=5)<result.evidence.available_at<=repo.now()
        assert repo.analysis_row('demo',body['id'])['decision_at']<=repo.now()


@pytest.mark.parametrize('status',[400,422])
@pytest.mark.parametrize('failure',['receive','identify_receipt'])
def test_invalid_payload_keeps_client_error_when_audit_fails(api,storage,monkeypatch,status,failure):
    def fail(*a,**kw):raise ConnectionError('Audit connection unavailable')
    monkeypatch.setattr(storage,failure,fail)
    body=payload()
    if status==422:body['source_account']['owner']['document_number']='12345678900'
    else:body['amount']=False
    r=post(api(),body)
    assert r.status_code==status,r.text
    assert r.json()['code']==('QIT002003' if status==422 else 'QIT000001')
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM analysis_claim')).scalar()==0


def test_known_execution_ingested_during_enrichment_is_not_reingested(api,storage,monkeypatch):
    old=payload('known-execution');old['transaction_date']='2026-10-14T12:00:00Z'
    _,p,dto=save(storage,payload=old)
    storage.link_bank_operation('demo',IDENTITY.name,'known-op',p.id,fingerprint(p))
    payment=BankPayment(IDENTITY.name,'known-op',replace(p,id='known-op'),
        ExecutionEvent('known-exec',p.payer,'known-op','executed',p.transaction_at,p.transaction_at))
    service=controller(storage,bank=FixtureBankProvider(IDENTITY,history=(payment,)))
    original=service._persist_enrichment;real_ingest=storage.ingest;duplicates=[]
    def arrived(enriched):
        real_ingest(event(p,dto,occurred=p.transaction_at,event_id='known-exec'))
        return original(enriched)
    def ingested(*a,**kw):
        duplicates.append(1)
        return real_ingest(*a,**kw)
    monkeypatch.setattr(service,'_persist_enrichment',arrived)
    monkeypatch.setattr(storage,'ingest',ingested)
    r=post(api(service),payload())
    assert r.status_code==201 and not duplicates,r.text
    # Already present at the first read: selection.py already deduplicated this
    # on the reviewed SHA. This part is coverage, not a newly discovered bug.
    monkeypatch.setattr(service,'_persist_enrichment',original)
    assert post(api(service),payload()).status_code==201
    assert not duplicates


@pytest.mark.parametrize('encoded,escaped',[('a%0AFAKE%20LINE',None),('a%E2%80%A8FAKE%20LINE','\\u2028')])
def test_decoded_path_and_query_cannot_inject_log_lines(api,caplog,encoded,escaped):
    url=api();response=requests.get(url+'/pix/transaction/'+encoded+'?x=%0A',headers={'INTERNAL-TOKEN':'test_token'},timeout=6)
    assert response.status_code==404
    entries=[r.getMessage() for r in caplog.records if r.name.endswith('request_logger')]
    assert entries
    assert all('\n' not in s and '\r' not in s and '\u2028' not in s for s in entries)
    if escaped is not None:
        assert any(escaped in s for s in entries)


def test_pix_budget_uses_its_own_pool_independent_of_base_connections(api,storage):
    from sqlalchemy.orm import sessionmaker
    from app import create_app
    from controllers.pix_storage import PixStorage
    from database import engine,pix_engine,pix_engine_for
    assert create_app().state.pix_controller.storage.sessions.kw['bind'] is pix_engine
    assert pix_engine is not engine and engine.pool.timeout()==30 and pix_engine.pool.timeout()==.1
    # The production Pix pool parameters, bound to this test's disposable database:
    # the shared sentinel database never receives Pix rows from the suite.
    isolated=pix_engine_for(storage.sessions.kw['bind'].url)
    assert ((isolated.pool.timeout(),isolated.pool.size(),isolated.pool._recycle,isolated.pool._pre_ping)
            ==(pix_engine.pool.timeout(),pix_engine.pool.size(),pix_engine.pool._recycle,pix_engine.pool._pre_ping))
    url=api(controller(PixStorage(sessionmaker(bind=isolated,autoflush=False))));body=payload()
    connections=[engine.connect() for _ in range(15)]
    try:
        # Exercise the Pix pool while every Base API connection is checked out.
        assert post(url,body).status_code==201
        assert connections[0].execute(text('SELECT count(*) FROM pix_transaction WHERE external_id=:id'),{'id':body['id']}).scalar()==0
        with storage.read() as repo:
            assert repo.analysis_row('demo',body['id']) is not None
    finally:
        for connection in connections:connection.close()
        isolated.dispose()


@pytest.mark.parametrize('different_hash,completed,expected',[(False,True,200),(True,True,409),(False,False,503)])
def test_actual_named_unique_reads_only_a_completed_winner(api,storage,monkeypatch,different_hash,completed,expected):
    from models import pix as m
    from antifraud.context import Context
    from antifraud.dto import PixTransactionDTO
    from antifraud.engine import evaluate
    from antifraud.policy import Policy
    _,template,_=save(storage,external='winner-template')
    body=payload();winning_body=deepcopy(body)
    if different_hash:winning_body['amount']+=1
    n=normalized(payload=winning_body);pix=to_pix(n,'demo');key=str(uuid4());inserted=[]
    original=storage.analysis
    @contextmanager
    def external_writer(*args,**kwargs):
        # Deliberately simulate a writer outside the coordinator protocol.
        # Insert before the final claim row lock so its FK can acquire KEY SHARE.
        with storage.write() as r:
            source=r.analysis_row('demo',template.id)
            values={col.name:source[col.name] for col in m.pix.c}
            values.update(id=uuid4(),external_id=pix.id,payload_hash=n.payload_hash,
                          payload=n.canonical_payload,amount_cents=pix.amount_cents)
            r.session.execute(m.pix.insert().values(**values))
            if completed:
                context=Context(pix,r.now());policy=Policy();decision=evaluate(context,policy)
                dto=PixTransactionDTO.from_decision(key,n,context,policy,decision)
                r.session.execute(m.analysis.insert().values(key=key,tenant_id='demo',pix_id=values['id'],
                    status=decision.status,decision_at=context.decision_at,policy_version=policy.version,
                    score=decision.score,device_state=decision.device_state,night_qualifies=decision.night_qualifies,
                    public_response=dto.public,snapshot=dto.audit))
                inserted.append(dto.public)
        with original(*args,**kwargs) as work:
            yield work  # The actual INSERT hits PostgreSQL 23505 in the named constraint.
    monkeypatch.setattr(storage,'analysis',external_writer)
    service=controller(storage);url=api(service);r=post(url,body)
    assert r.status_code==expected,r.text
    if expected==200:
        assert r.json()==inserted[0]
        class NoProvider:
            identity=IDENTITY
            async def security(self,*a,**kw):raise AssertionError('Completed winner must replay without provider')
        service.bank=NoProvider()
        replay=post(url,body)
        assert replay.status_code==200 and replay.json()==r.json()
    elif expected==409:
        assert r.json()['code']=='QIT002002'
    else:
        assert get(url,body['id']).status_code==404


@pytest.mark.parametrize('write',[False,True])
def test_statement_and_lock_timeouts_follow_remaining_budget(storage,write):
    from providers.budget import Deadline
    from utils.pix_deadline import in_database
    deadline=Deadline.after(.5)
    def read_limits():
        transaction=storage.write(lock_timeout_ms=37) if write else storage.read()
        with transaction as r:
            results=[]
            for _ in range(2):
                sleep(.02)
                before=deadline.remaining()*1000
                settings=r.session.execute(text("SELECT current_setting('statement_timeout'),current_setting('lock_timeout')")).one()
                after=deadline.remaining()*1000
                statement,lock=(int(value.removesuffix('ms')) for value in settings)
                assert after-1<=statement<=before+1
                assert lock==min(statement,37 if write else 1000)
                results.append(statement)
            assert results[1]<results[0]-10
    asyncio.run(in_database(deadline,read_limits))


def test_content_length_non_numeric_is_400_at_resource_boundary(storage):
    # Uvicorn rejects this before ASGI; directly exercise the Resource guard too.
    from app import create_app
    from starlette.requests import Request
    from resources.pix import PixResource
    from utils.pix_tenants import Tenant
    from errors.pix import PixError
    from providers.budget import Deadline
    app=create_app();app.state.pix_controller=controller(storage)
    scope={'type':'http','method':'POST','path':'/pix/transaction','headers':[(b'content-length',b'not-a-number')],
           'query_string':b'','app':app,'state':{'pix_tenant':Tenant('demo','test_token','00000000'),
           'pix_deadline':Deadline.after(3),'request_id':'bad-length'}}
    with pytest.raises(PixError) as error:
        asyncio.run(PixResource().on_post(Request(scope)))
    assert error.value.http_status==400 and error.value.code=='QIT000001'
    with storage.read() as r:
        assert r.session.execute(text('SELECT http_status FROM request_receipt')).scalar()==400


def test_two_tenants_load_through_create_app_and_http(api,storage,tmp_path,monkeypatch):
    from antifraud.configuration import DEFAULT_POLICY_PATH
    config=json.loads(DEFAULT_POLICY_PATH.read_text())
    config['tenants']['other']=config['tenants']['demo']
    path=tmp_path/'two-tenants.json';path.write_text(json.dumps(config))
    monkeypatch.setenv('PIX_POLICY_FILE',str(path))
    monkeypatch.setenv('PIX_TENANTS',json.dumps({'demo':{'token':'configured-a','participant':'00000000'},
                                                'other':{'token':'configured-b','participant':'11111111'}}))
    url=api(controller(storage),configured=True);body=payload()
    a=post(url,body,'configured-a');b=post(url,body,'configured-b')
    assert a.status_code==b.status_code==201,(a.text,b.text)
    assert a.json()['key']!=b.json()['key']
    assert post(url,body,'test_token').status_code==403
    with storage.read() as r:
        assert r.analysis_row('demo',body['id'])['snapshot']['source_participant']=='00000000'
        assert r.analysis_row('other',body['id'])['snapshot']['source_participant']=='11111111'


def test_releasing_conflict_reservations_cannot_expire_another_owner(storage):
    from tests.integration.test_pix_storage import expire
    body=payload();n=normalized(payload=body);p=to_pix(n,'demo')
    old=storage.claim('demo',p.id,n.payload_hash);old_initial=storage.claim_initial(old,p.payer)
    expire(storage,'analysis_claim',tenant_id='demo',external_id=p.id)
    new=storage.claim('demo',p.id,n.payload_hash)
    storage.release_reservations(old,old_initial)
    with storage.read() as r:
        assert r.session.execute(text('SELECT lease_until>clock_timestamp() FROM analysis_claim')).scalar()
        assert r.session.execute(text('SELECT lease_until>clock_timestamp() FROM source_initial_check')).scalar()
    expire(storage,'source_initial_check',tenant_id='demo',branch=p.payer.branch,account=p.payer.account,document=p.payer.document)
    new_initial=storage.claim_initial(new,p.payer)
    storage.release_reservations(old,old_initial)
    with storage.read() as r:
        assert r.session.execute(text('SELECT lease_until FROM analysis_claim')).scalar()==new.lease_until
        assert r.session.execute(text('SELECT lease_until FROM source_initial_check')).scalar()==new_initial.lease_until
    storage.release_reservations(new,new_initial)
    with storage.read() as r:
        assert r.session.execute(text('SELECT lease_until<=clock_timestamp() FROM analysis_claim')).scalar()
        assert r.session.execute(text('SELECT lease_until<=clock_timestamp() FROM source_initial_check')).scalar()
        assert r.session.execute(text('SELECT payload_hash FROM analysis_claim')).scalar()==n.payload_hash


def test_clock_fix_does_not_accept_a_source_result_known_in_the_future(storage):
    from tests.unit.test_pix_provider_selection import reputation_record
    body=payload();n=normalized(payload=body);p=to_pix(n,'demo')
    claim=storage.claim('demo',p.id,n.payload_hash);initial=storage.claim_initial(claim,p.payer)
    with storage.read() as r:future=r.now()+timedelta(days=1)
    result=reputation_record(p.payer.document,observed_at=future,available_at=future,valid_until=future+timedelta(minutes=5))
    with pytest.raises(ValueError,match='future coverage'):
        storage.complete_initial(initial,result)
    assert storage.initial(p.payer) is None
