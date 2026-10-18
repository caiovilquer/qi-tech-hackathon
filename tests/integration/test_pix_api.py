"""Stage 5: real HTTP server and PostgreSQL, including concurrent requests."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from os import environ
from socket import socket
from threading import Thread, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
import requests
import uvicorn
from sqlalchemy import text

from tests.integration.test_pix_storage import storage, normalized, expire, event
from tests.integration.test_migrations import migration_db
from tests.unit.test_pix_contract import OFFICIAL_PAYLOAD
from antifraud.adapter import to_pix
from antifraud.configuration import PolicyCatalog
from antifraud.policy import Policy
from controllers.pix_api import PixController
from utils.pix_tenants import Tenant
from providers.contracts import ProviderIdentity
from providers.fixtures import FixtureBankProvider, FixtureReputationProvider
from utils.pix_storage_codec import fingerprint

IDENTITY = ProviderIdentity('bank-demo', 'v1')


def controller(storage, bank=None, reputation=None):
    return PixController(storage, bank or FixtureBankProvider(IDENTITY),
                         reputation or FixtureReputationProvider(IDENTITY))


@pytest.fixture
def api(storage):
    from app import create_app
    servers = []
    def start(service=None, *, configured=False):
        app = create_app()
        app.state.pix_controller = service or controller(storage)
        if not configured:
            app.state.pix_tenants = (Tenant('demo', 'test_token', '00000000'), Tenant('other', 'other-token', '11111111'))
            app.state.pix_policies = PolicyCatalog(((Policy().version, Policy()),),
                                                  (('demo', Policy().version), ('other', Policy().version)))
        sock = socket(); sock.bind(('127.0.0.1', 0)); sock.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
        thread = Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True); thread.start()
        until = monotonic()+5
        while not server.started:
            if not thread.is_alive() or monotonic()>until:
                raise RuntimeError('Test HTTP server failed to start')
            sleep(.01)
        servers.append((server,thread,sock))
        return 'http://127.0.0.1:'+str(sock.getsockname()[1])
    yield start
    for server,thread,sock in servers:
        server.should_exit=True; thread.join(5); sock.close()
        assert not thread.is_alive()


def payload(id=None,amount=5000):
    p=deepcopy(OFFICIAL_PAYLOAD); p['id']=id or uuid4().hex; p['amount']=amount
    return p


def post(url,p,token='test_token',**kwargs):
    return requests.post(url+'/pix/transaction',data=json.dumps(p),
                         headers={'INTERNAL-TOKEN':token},timeout=6,**kwargs)


def get(url,id,token='test_token'):
    from urllib.parse import quote
    return requests.get(url+'/pix/transaction/'+quote(id,safe=''),headers={'INTERNAL-TOKEN':token},timeout=6)


@pytest.mark.parametrize('amount,status',[(5000,'approved'),(150000,'two_factor_required'),(100000000000,'rejected')])
def test_official_http_decisions_are_committed_and_queryable(api,storage,amount,status):
    url=api(); p=payload(amount=amount); r=post(url,p)
    assert r.status_code==201, r.text
    assert set(r.json())=={'key','status','metadata'}
    assert r.json()['status']==status
    assert get(url,p['id']).json()==r.json()
    with storage.read() as repo:
        row=repo.analysis_row('demo',p['id'])
        assert row['amount_cents']==amount
        assert row['snapshot']['source_participant']=='00000000'
        assert repo.session.execute(text('SELECT http_status FROM request_receipt')).scalar()==201


def test_replay_get_restart_and_tenant_are_independent_of_providers(api,storage):
    url=api(); p=payload('pix % unicode-é'); r=post(url,p); assert r.status_code==201,r.text
    class ForbiddenBank:
        identity=IDENTITY
        async def security(self,*args,**kwargs):
            raise AssertionError('Replay must not call providers')
    fresh=api(controller(storage,bank=ForbiddenBank()))
    replay=post(fresh,p)
    assert replay.status_code==200 and replay.json()==r.json()
    assert get(fresh,p['id']).json()==r.json()
    assert get(fresh,p['id'],'other-token').status_code==404
    independent=post(url,p,'other-token')
    assert independent.status_code==201 and independent.json()['key']!=r.json()['key']
    changed=deepcopy(p); changed['source']['channel']='internet'
    conflict=post(fresh,changed)
    assert conflict.status_code==409 and conflict.json()['code']=='QIT002002'
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM pix_transaction')).scalar()==2


@pytest.mark.parametrize('raw,status,code',[
    (b'{',400,'QIT000001'),(b'{"id":"a","id":"b"}',400,'QIT000001'),
    (b'NaN',400,'QIT000001'),(b' '*65537,413,'QIT002009'),
    (json.dumps(payload()).replace('5000','5000.0000000000001').encode(),400,'QIT000001'),
])
def test_http_raw_parser_errors_leave_receipts_without_analysis(api,storage,raw,status,code):
    r=requests.post(api()+'/pix/transaction',data=raw,headers={'INTERNAL-TOKEN':'test_token'},timeout=6)
    assert r.status_code==status and r.json()['code']==code,r.text
    assert set(r.json())=={'title','description','translation','code'}
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==0
        assert repo.session.execute(text('SELECT http_status FROM request_receipt')).scalar()==status


@pytest.mark.parametrize('field,value,code',[
    ('document','12345678900','QIT002003'),('date','2026-02-30T00:00:00Z','QIT002004'),
    ('opening','2027-01-01T00:00:00Z','QIT002005')])
def test_http_semantic_errors(api,storage,field,value,code):
    p=payload()
    if field=='document':p['source_account']['owner']['document_number']=value
    elif field=='date':p['transaction_date']=value
    else:p['source_account']['opening_date']=value
    r=post(api(),p); assert r.status_code==422 and r.json()['code']==code,r.text
    with storage.read() as repo:
        receipt=repo.session.execute(text('SELECT external_id,payload_hash FROM request_receipt')).one()
        assert receipt.external_id==p['id'] and receipt.payload_hash is None


def test_http_auth_and_unfinished_get(api,storage):
    url=api();p=payload();n=normalized(payload=p)
    storage.claim('demo',p['id'],n.payload_hash)
    assert get(url,p['id']).status_code==404
    for token in ('','bad'):
        r=post(url,p,token);assert r.status_code==403 and r.json()['code']=='QIT000002'
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM request_receipt WHERE tenant_id IS NULL')).scalar()==0


def test_http_concurrent_same_id_commits_once_and_different_hash_is_409(api,storage):
    url=api();p=payload()
    with ThreadPoolExecutor(max_workers=4) as pool:
        rs=list(pool.map(lambda _:post(url,p),range(4)))
    assert sorted(r.status_code for r in rs)==[200,200,200,201],[(r.status_code,r.text) for r in rs]
    assert all(r.json()==rs[0].json() for r in rs)
    q=deepcopy(p);q['amount']+=1
    assert post(url,q).status_code==409
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==1
        assert repo.session.execute(text('SELECT count(*) FROM source_initial_check')).scalar()==1


def test_http_busy_claim_preserves_hash_and_takeover_recovers(api,storage):
    url=api();p=payload();n=normalized(payload=p);old=storage.claim('demo',p['id'],n.payload_hash)
    changed=deepcopy(p);changed['amount']+=1
    start=monotonic(); assert post(url,changed).status_code==409; assert monotonic()-start<1
    start=monotonic();r=post(url,p);elapsed=monotonic()-start
    assert r.status_code==503 and r.json()['code']=='QIT002008'
    assert elapsed<3.5
    expire(storage,'analysis_claim',tenant_id='demo',external_id=p['id'])
    assert post(url,p).status_code==201
    from dtos.pix_storage import LostClaim
    with pytest.raises(LostClaim),storage.analysis(old,to_pix(n,'demo')):
        raise AssertionError('Old owner must not enter')


def test_source_initial_survives_later_provider_failure_and_next_id_skips_it(api,storage):
    from providers.contracts import ProviderDataError
    from providers.fixtures import FixtureReputationProvider
    calls=[]
    class BrokenPayee:
        identity=IDENTITY
        async def reputation(self,key,as_of,**kw):
            calls.append(key.document)
            if key.document=='98765432100':
                raise ProviderDataError('Invalid payee record')
            return await FixtureReputationProvider(IDENTITY).reputation(key,as_of,**kw)
    service=controller(storage,reputation=BrokenPayee());url=api(service);p=payload()
    assert post(url,p).status_code==503
    n=normalized(payload=p);assert storage.initial(to_pix(n,'demo').payer) is not None
    with storage.read() as r:
        assert r.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==0
    p['id']=uuid4().hex
    assert post(url,p).status_code==503
    assert calls.count('12345678909')==1


def test_http_commit_failure_has_no_partial_analysis_and_unknown_commit_replays(api,storage,monkeypatch):
    from contextlib import contextmanager
    service=controller(storage);url=api(service);original=storage.analysis
    @contextmanager
    def failed(*args,**kwargs):
        with original(*args,**kwargs) as work:
            # A real deferred PostgreSQL trigger fails during COMMIT.
            work.repository.session.execute(text('''CREATE FUNCTION fail_final() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'forced deferred commit failure'; END; $$'''))
            work.repository.session.execute(text('''CREATE CONSTRAINT TRIGGER fail_final AFTER INSERT ON risk_analysis
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fail_final()'''))
            yield work
    monkeypatch.setattr(storage,'analysis',failed)
    p=payload();r=post(url,p);assert r.status_code==503
    assert get(url,p['id']).status_code==404
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM pix_transaction')).scalar()==0
        assert repo.session.execute(text("SELECT count(*) FROM source_initial_check WHERE state='completed'")).scalar()==1
    monkeypatch.setattr(storage,'analysis',original)
    expire(storage,'analysis_claim',tenant_id='demo',external_id=p['id'])
    @contextmanager
    def unknown(*args,**kwargs):
        with original(*args,**kwargs) as work:
            yield work
        raise ConnectionError('Acknowledgement lost after a real commit')
    monkeypatch.setattr(storage,'analysis',unknown)
    assert post(url,p).status_code==503
    committed=get(url,p['id']);assert committed.status_code==200
    monkeypatch.setattr(storage,'analysis',original)
    replay=post(url,p);assert replay.status_code==200 and replay.json()==committed.json()


def test_http_distinct_origin_and_destination_requests_serialize_fan_in(api,storage):
    url=api();ps=[payload(amount=100000) for _ in range(5)]
    for i,p in enumerate(ps):p['source_account']['account_number']=str(100+i)
    with ThreadPoolExecutor(max_workers=5) as pool:
        rs=list(pool.map(lambda p:post(url,p),ps))
    assert all(r.status_code==201 for r in rs),[(r.status_code,r.text) for r in rs]
    with storage.read() as repo:
        snapshots=repo.session.execute(text('SELECT snapshot FROM risk_analysis ORDER BY sequence')).scalars().all()
        assert [s['features']['fan_in_payers'] for s in snapshots]==[1,2,3,4,5]
        assert 'MULE_FAN_IN' in [r['code'] for r in snapshots[-1]['decision']['reasons']]


def test_same_origin_distinct_requests_are_all_counted_after_initial_wait(api,storage):
    url=api();ps=[payload(amount=5000) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        rs=list(pool.map(lambda p:post(url,p),ps))
    assert all(r.status_code==201 for r in rs),[(r.status_code,r.text) for r in rs]
    with storage.read() as repo:
        ss=repo.session.execute(text('SELECT snapshot FROM risk_analysis ORDER BY sequence')).scalars().all()
        assert [s['features']['receipt_count'] for s in ss]==[1,2,3]
        assert repo.session.execute(text('SELECT count(*) FROM source_initial_check')).scalar()==1


def test_db_deadline_cancels_statement_and_prevents_late_commit(api,storage,monkeypatch):
    service=controller(storage);url=api(service)
    original=service._finalize_analysis
    def slow(*args,**kwargs):
        # The actual slow SQL executes within the final transaction, not a mock repository.
        original_context=storage.analysis
        from contextlib import contextmanager
        @contextmanager
        def blocked(*a,**kw):
            with original_context(*a,**kw) as work:
                work.repository.session.execute(text('SELECT pg_sleep(5)'))
                yield work
        storage.analysis=blocked
        try:return original(*args,**kwargs)
        finally:storage.analysis=original_context
    monkeypatch.setattr(service,'_finalize_analysis',slow)
    p=payload();start=monotonic();r=post(url,p)
    assert r.status_code==503 and monotonic()-start<3.5
    sleep(.15)
    assert get(url,p['id']).status_code==404
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==0


def test_provider_outage_is_uncertainty_not_fake_clean_or_503(api,storage):
    from providers.contracts import ProviderUnavailable
    class Outage:
        identity=IDENTITY
        async def security(self,*a,**kw):raise ProviderUnavailable('outage')
        async def payments(self,*a,**kw):raise ProviderUnavailable('outage')
        async def reputation(self,*a,**kw):raise ProviderUnavailable('outage')
    # Each protocol is the actual async interface; technical fallback is tested through HTTP.
    r=post(api(controller(storage,Outage(),Outage())),payload())
    assert r.status_code==201,r.text
    assert r.json()['status']=='approved'
    assert 'DICT_CHECK_UNAVAILABLE' in r.json()['metadata']['reasons']
    assert r.json()['metadata']['enrichment']['payer']['state']=='unavailable'


def test_bank_link_arriving_after_enrichment_does_not_count_execution_twice(api,storage,monkeypatch):
    from tests.integration.test_pix_storage import save
    from antifraud.context import BankPayment, ExecutionEvent
    from utils.pix_storage_codec import fingerprint
    old=payload('old-linked');old['transaction_date']='2026-10-14T12:00:00Z'
    _,p,dto=save(storage,payload=old)
    payment=BankPayment(IDENTITY.name,'bank-op',replace(p,id='bank-op'),
                       ExecutionEvent('bank-exec',p.payer,'bank-op','executed',p.transaction_at,p.transaction_at))
    service=controller(storage,bank=FixtureBankProvider(IDENTITY,history=(payment,)))
    persist=service._persist_enrichment
    def arrived(enriched):
        persist(enriched)
        storage.link_bank_operation('demo',IDENTITY.name,'bank-op',p.id,fingerprint(p))
        storage.ingest(event(p,dto,occurred=p.transaction_at,event_id='bank-exec'))
    monkeypatch.setattr(service,'_persist_enrichment',arrived)
    r=post(api(service),payload())
    assert r.status_code==201,r.text
    with storage.read() as repo:
        snapshots=repo.session.execute(text('SELECT snapshot FROM risk_analysis ORDER BY sequence DESC LIMIT 1')).scalar()
    assert snapshots['features']['mature_payments']==1
    assert snapshots['features']['bank_payment_count']==0


def test_imported_history_is_frozen_in_private_audit_without_api_attempt(api,storage):
    from antifraud.context import BankPayment, ExecutionEvent
    p=to_pix(normalized(payload=payload('bank-original')),'demo')
    p=replace(p,transaction_at=p.transaction_at-timedelta(days=3))
    payment=BankPayment(IDENTITY.name,p.id,p,ExecutionEvent('exec-import',p.payer,p.id,'executed',p.transaction_at,p.transaction_at))
    r=post(api(controller(storage,bank=FixtureBankProvider(IDENTITY,history=(payment,)))),body:=payload())
    assert r.status_code==201,r.text
    with storage.read() as repo:
        audit=repo.analysis_row('demo',body['id'])['snapshot']
        assert audit['bank_payments'][0]['operation_id']==p.id
        assert audit['bank_payments'][0]['pix']['amount_cents']==p.amount_cents
        assert repo.session.execute(text('SELECT count(*) FROM pix_transaction')).scalar()==1
        assert repo.session.execute(text('SELECT count(*) FROM request_receipt')).scalar()==1


def test_out_of_window_linked_bank_history_does_not_break_new_request(api,storage):
    from tests.integration.test_pix_storage import save
    from antifraud.context import BankPayment, ExecutionEvent
    p=payload('ancient');p['transaction_date']='2026-01-02T12:00:00Z'
    p['destination_account']['opening_date']='2025-01-01T00:00:00Z'
    _,old,dto=save(storage,payload=p)
    storage.link_bank_operation('demo',IDENTITY.name,'bank-ancient',old.id,fingerprint(old))
    row=BankPayment(IDENTITY.name,'bank-ancient',replace(old,id='bank-ancient'),
        ExecutionEvent('ancient-exec',old.payer,'bank-ancient','executed',old.transaction_at,old.transaction_at))
    r=post(api(controller(storage,bank=FixtureBankProvider(IDENTITY,history=(row,)))),payload())
    assert r.status_code==201,r.text


def test_known_expired_bank_context_never_persists_after_three_cycles(api,storage):
    from tests.unit.test_pix_provider_selection import security_record
    p=payload(); pix=to_pix(normalized(payload=p),'demo');now=datetime.now(timezone.utc)
    record=security_record(pix,observed_at=now-timedelta(hours=1),available_at=now-timedelta(hours=1),valid_until=now-timedelta(seconds=1))
    calls=[]
    class StaleBank(FixtureBankProvider):
        async def security(self,*a,**kw):
            calls.append(1)
            return await super().security(*a,**kw)
    service=controller(storage,StaleBank(IDENTITY,records=(record,)))
    r=post(api(service),p)
    assert r.status_code==503 and len(calls)==3
    assert storage.initial(pix.payer) is not None
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==0


def test_new_bank_link_is_ingested_outside_locks_on_next_cycle(api,storage,monkeypatch):
    from tests.integration.test_pix_storage import save
    from antifraud.context import BankPayment, ExecutionEvent
    old=payload('new-link');old['transaction_date']='2026-10-14T12:00:00Z'
    _,p,dto=save(storage,payload=old)
    payment=BankPayment(IDENTITY.name,'late-op',replace(p,id='late-op'),
        ExecutionEvent('late-exec',p.payer,'late-op','executed',p.transaction_at,p.transaction_at))
    service=controller(storage,bank=FixtureBankProvider(IDENTITY,history=(payment,)))
    persist=service._persist_enrichment;calls=[]
    def arrive(enriched):
        persist(enriched);calls.append(1)
        storage.link_bank_operation('demo',IDENTITY.name,'late-op',p.id,fingerprint(p))
    monkeypatch.setattr(service,'_persist_enrichment',arrive)
    body=payload();r=post(api(service),body)
    assert r.status_code==201 and len(calls)==2,r.text
    with storage.read() as repo:
        assert repo.analysis_row('demo',body['id'])['snapshot']['features']['mature_payments']==1
        assert repo.session.execute(text('SELECT count(*) FROM bank_event')).scalar()==1


def test_locked_security_refresh_recomputes_preliminary_recipient_and_rejects(api,storage,monkeypatch):
    from antifraud.context import BankPayment, ExecutionEvent
    from tests.unit.test_pix_provider_selection import security_record
    from providers.fixtures import FixtureReputationProvider
    body=payload();pix=to_pix(normalized(payload=body),'demo');now=datetime.now(timezone.utc)
    security=security_record(pix,observed_at=now-timedelta(hours=1),available_at=now-timedelta(hours=1),valid_until=now+timedelta(minutes=5))
    payments=[]
    for i in range(20):
        p=replace(pix,id='historic-'+str(i),transaction_at=pix.transaction_at-timedelta(days=3+i),destination_opened_at=pix.transaction_at-timedelta(days=60))
        payments.append(BankPayment(IDENTITY.name,p.id,p,ExecutionEvent('exec-'+p.id,p.payer,p.id,'executed',p.transaction_at,p.transaction_at)))
    calls=[]
    class Reputation(FixtureReputationProvider):
        async def reputation(self,key,*a,**kw):
            calls.append(key.document)
            return await super().reputation(key,*a,**kw)
    service=controller(storage,FixtureBankProvider(IDENTITY,records=(security,),history=tuple(payments)),Reputation(IDENTITY))
    persist=service._persist_enrichment;cycles=[]
    def revoke(enriched):
        persist(enriched);cycles.append(1)
        if len(cycles)==1:
            revoked=replace(security,snapshot=replace(security.snapshot,source_version=1,device_state='revoked',
                observed_at=now-timedelta(seconds=1),available_at=now-timedelta(seconds=1),
                recoveries=((now-timedelta(minutes=5),now-timedelta(minutes=5)),)))
            with storage.write() as r:r.put_snapshots((revoked,))
    monkeypatch.setattr(service,'_persist_enrichment',revoke)
    r=post(api(service),body)
    assert r.status_code==201 and r.json()['status']=='rejected',r.text
    assert len(cycles)==2 and calls.count(pix.payee_document)==1
    with storage.read() as repo:
        snap=repo.analysis_row('demo',body['id'])['snapshot']
        assert snap['security']['source_version']==1
        assert snap['enrichment']['payee']['action']=='queried'


def test_receiving_chunked_body_enforces_limit_and_accepts_exact_boundary(api,storage):
    url=api();raw=json.dumps(payload()).encode();raw+=b' '*(65536-len(raw))
    good=requests.post(url+'/pix/transaction',data=(raw[i:i+3000] for i in range(0,len(raw),3000)),headers={'INTERNAL-TOKEN':'test_token'},timeout=6)
    assert good.status_code==201,good.text
    bad=requests.post(url+'/pix/transaction',data=iter((b' '*40000,b' '*30000)),headers={'INTERNAL-TOKEN':'test_token'},timeout=6)
    assert bad.status_code==413 and bad.json()['code']=='QIT002009'
    with storage.read() as r:
        assert r.session.execute(text('SELECT count(*) FROM request_receipt WHERE http_status=413')).scalar()==1


def test_actual_process_crash_and_restart_recover_both_leases(storage,tmp_path):
    import subprocess,sys
    processes=[]
    def launch(block=False):
        with socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        marker=tmp_path/'provider-entered'
        script='''
import asyncio, os, uvicorn
from app import app
from providers.fixtures import FixtureBankProvider
from pathlib import Path
if os.environ.get('BLOCK_PROVIDER'):
    class Blocked(FixtureBankProvider):
        async def security(self,*a,**kw):
            Path(os.environ['PROVIDER_MARKER']).write_text('entered')
            await asyncio.sleep(5)
            return await super().security(*a,**kw)
    app.state.pix_controller.bank=Blocked(app.state.pix_controller.bank.identity)
uvicorn.run(app,host='127.0.0.1',port=int(os.environ['PROCESS_PORT']),log_level='error',lifespan='off')
'''
        env=dict(environ,DATABASE_URL=storage.sessions.kw['bind'].url.render_as_string(hide_password=False),
            INTERNAL_TOKEN='test_token',PIX_TENANTS='',PIX_FIXTURE_FILE='',PYTHONPATH='/app:/workspace:/opt/pix',
            PROCESS_PORT=str(port),PROVIDER_MARKER=str(marker),BLOCK_PROVIDER='1' if block else '')
        process=subprocess.Popen([sys.executable,'-c',script],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        processes.append(process);url='http://127.0.0.1:'+str(port)
        until=monotonic()+5
        while True:
            try:
                if requests.get(url+'/health_check',timeout=.2).status_code==204:break
            except requests.ConnectionError:pass
            if process.poll() is not None or monotonic()>until:raise RuntimeError('Child server did not start')
            sleep(.02)
        return url,process,marker
    try:
        url,worker,marker=launch(True);body=payload();n=normalized(payload=body);pix=to_pix(n,'demo')
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(post,url,body)
            until=monotonic()+3
            while not marker.exists():
                assert monotonic()<until
                sleep(.01)
            worker.kill();worker.wait(3)
            with pytest.raises(requests.ConnectionError):pending.result()
        with storage.read() as r:
            assert r.session.execute(text("SELECT state FROM analysis_claim")).scalar()=='in_progress'
            assert r.session.execute(text("SELECT state FROM source_initial_check")).scalar()=='in_progress'
        expire(storage,'analysis_claim',tenant_id='demo',external_id=pix.id)
        expire(storage,'source_initial_check',tenant_id='demo',branch=pix.payer.branch,account=pix.payer.account,document=pix.payer.document)
        second,worker,_=launch();r=post(second,body);assert r.status_code==201,r.text
        worker.terminate();worker.wait(5)
        third,_,_=launch();replay=post(third,body)
        assert replay.status_code==200 and replay.json()==r.json()
        assert get(third,body['id']).json()==r.json()
        with storage.read() as repo:
            assert repo.session.execute(text('SELECT fencing_token FROM analysis_claim')).scalar()==2
            assert repo.session.execute(text('SELECT fencing_token FROM source_initial_check')).scalar()==2
    finally:
        for process in processes:
            if process.poll() is None:process.terminate()
            try:process.wait(5)
            except subprocess.TimeoutExpired:process.kill();process.wait(3)


def test_get_corrupt_stored_data_is_503_not_missing(api,storage,monkeypatch):
    def corrupt(*args):raise ValueError('Stored codec is corrupt')
    monkeypatch.setattr(storage,'get',corrupt)
    r=get(api(),'some-id')
    assert r.status_code==503 and r.json()['code']=='QIT002008'


def test_unrelated_unique_constraint_is_503_not_replay(api,storage,monkeypatch):
    from contextlib import contextmanager
    original=storage.analysis
    @contextmanager
    def failed(*args,**kwargs):
        with original(*args,**kwargs) as work:
            work.repository.session.execute(text('''CREATE FUNCTION different_unique() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'forced unique failure' USING ERRCODE='23505', CONSTRAINT='different_unique'; END; $$'''))
            work.repository.session.execute(text('''CREATE CONSTRAINT TRIGGER different_unique AFTER INSERT ON risk_analysis
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION different_unique()'''))
            yield work
    monkeypatch.setattr(storage,'analysis',failed)
    body=payload();url=api();r=post(url,body)
    assert r.status_code==503 and get(url,body['id']).status_code==404


def test_terminal_2fa_event_arriving_before_final_locks_releases_only_its_exposure(api,storage):
    import asyncio
    from tests.integration.test_pix_storage import save
    old=payload('twofa-old',amount=150000)
    _,p,dto=save(storage,payload=old)
    assert dto.public['status']=='two_factor_required'
    storage.bind_challenge('demo',IDENTITY.name,p.id,dto.public['key'],'challenge',fingerprint(p))
    entered,release=Event(),Event()
    class Paused(FixtureBankProvider):
        async def security(self,*a,**kw):
            entered.set()
            await asyncio.to_thread(release.wait,.2)
            return await super().security(*a,**kw)
    url=api(controller(storage,bank=Paused(IDENTITY)));body=payload()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(post,url,body)
        assert entered.wait(2)
        storage.ingest(event(p,dto,'failed',kind='auth',challenge='challenge',event_id='terminal-failed'))
        release.set();r=pending.result(4)
    assert r.status_code==201,r.text
    with storage.read() as repo:
        audit=repo.analysis_row('demo',body['id'])['snapshot']
        assert audit['features']['night_soft_exposure_cents']==0
        assert repo.get('demo',p.id).public==dto.public


def test_incomplete_chunked_body_has_bounded_503_and_error_receipt(api,storage):
    from urllib.parse import urlparse
    target=urlparse(api())
    with socket() as client:
        client.settimeout(4)
        client.connect((target.hostname,target.port))
        client.sendall(b'POST /pix/transaction HTTP/1.1\r\nHost: localhost\r\nINTERNAL-TOKEN: test_token\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n')
        started=monotonic();response=client.recv(4096)
        assert b' 503 ' in response and monotonic()-started<3.5
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT http_status FROM request_receipt')).scalar()==503


def test_replay_receipt_storage_failure_is_503(api,storage,monkeypatch):
    url=api();body=payload();assert post(url,body).status_code==201
    def failure(*args,**kwargs):raise ConnectionError('Receipt write failed')
    monkeypatch.setattr(storage,'finish_receipt',failure)
    r=post(url,body)
    assert r.status_code==503 and r.json()['code']=='QIT002008'


def test_unreachable_postgresql_returns_503_and_does_not_invent_receipt(api,storage):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from controllers.pix_storage import PixStorage
    with socket() as unavailable:
        unavailable.bind(('127.0.0.1',0))
        engine=create_engine('postgresql+psycopg2://synthetic:synthetic@127.0.0.1:'+str(unavailable.getsockname()[1])+'/pix_test_unreachable',
                             connect_args={'connect_timeout':1},pool_timeout=.1)
        try:
            url=api(controller(PixStorage(sessionmaker(bind=engine))))
            r=post(url,payload());assert r.status_code==503 and r.json()['code']=='QIT002008'
            assert get(url,'unknown').status_code==503
            invalid=payload();invalid['source_account']['owner']['document_number']='12345678900'
            assert post(url,invalid).status_code==422
            invalid['amount']=False
            assert post(url,invalid).status_code==400
            assert post(url,payload(),'bad-token').status_code==403
        finally:engine.dispose()
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM request_receipt')).scalar()==0


def test_future_knowledge_bank_execution_is_not_ingested_as_known_now(api,storage):
    from tests.integration.test_pix_storage import save
    from antifraud.context import BankPayment, ExecutionEvent
    old=payload('future-knowledge');old['transaction_date']='2026-10-14T12:00:00Z'
    _,p,dto=save(storage,payload=old)
    storage.link_bank_operation('demo',IDENTITY.name,'future-op',p.id,fingerprint(p))
    payment=BankPayment(IDENTITY.name,'future-op',replace(p,id='future-op'),
        ExecutionEvent('future-exec',p.payer,'future-op','executed',p.transaction_at,datetime.now(timezone.utc)+timedelta(days=1)))
    class FutureBank(FixtureBankProvider):
        async def payments(self,*a,**kw):return (payment,)
    service=controller(storage,bank=FutureBank(IDENTITY));body=payload()
    r=post(api(service),body)
    assert r.status_code==201,r.text
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM bank_event')).scalar()==0
        assert repo.analysis_row('demo',body['id'])['snapshot']['features']['mature_payments']==0


def test_flush_sql_failure_is_rolled_back_before_http_success(api,storage,monkeypatch):
    from contextlib import contextmanager
    original=storage.analysis
    @contextmanager
    def broken(*args,**kwargs):
        with original(*args,**kwargs) as work:
            def fail_flush():
                work.repository.session.execute(text('SELECT 1/0'))
            work.repository.session.flush=fail_flush
            yield work
    monkeypatch.setattr(storage,'analysis',broken)
    body=payload();url=api();r=post(url,body)
    assert r.status_code==503
    assert get(url,body['id']).status_code==404
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM pix_transaction')).scalar()==0
        assert repo.session.execute(text("SELECT state FROM analysis_claim")).scalar()=='in_progress'
        assert repo.session.execute(text('SELECT http_status FROM request_receipt')).scalar()==503


def test_slow_providers_are_cancelled_without_blocking_http_event_loop(api,storage):
    import asyncio
    entered=Event();cancelled=[]
    class Slow:
        identity=IDENTITY
        async def wait(self,*a,**kw):
            entered.set()
            try:await asyncio.sleep(10)
            finally:cancelled.append(1)
        security=payments=reputation=wait
    url=api(controller(storage,Slow(),Slow()))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(post,url,payload())
        assert entered.wait(2)
        start=monotonic();healthy=requests.get(url+'/health_check',timeout=1)
        assert healthy.status_code==204 and monotonic()-start<.5
        response=pending.result(4)
    assert response.status_code==201,response.text
    assert response.json()['metadata']['enrichment']['payer']['state']=='unavailable'
    assert len(cancelled)==8  # security, history, payer, payee: one technical retry each.


def test_get_id_that_cannot_have_been_stored_is_404(api):
    r=get(api(),'a'*129)
    assert r.status_code==404 and r.json()['code']=='QIT002001'
