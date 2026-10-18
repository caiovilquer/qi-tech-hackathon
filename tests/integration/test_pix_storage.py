"""Real PostgreSQL contracts for stage 4; no Pix HTTP routes or fake persistence."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from migrations.runner import upgrade
from tests.integration.test_migrations import migration_db
from tests.unit.test_pix_contract import OFFICIAL_PAYLOAD
from tests.unit.test_pix_provider_selection import IDENTITY, reputation_record, security_record
from antifraud.adapter import to_pix
from antifraud.contract import normalize_payload
from antifraud.context import Context
from antifraud.policy import Policy
from antifraud.engine import evaluate
from controllers.pix_storage import PixStorage
from dtos.pix_storage import (Busy, LostClaim, HashConflict, Replay, OwnershipConflict,
                              EventInput, EventConflict, InvalidEvent, RecordConflict)
from utils.pix_storage_codec import pack, unpack, fingerprint


@pytest.fixture
def storage(migration_db):
    upgrade(migration_db)
    return PixStorage(sessionmaker(bind=migration_db, autoflush=False))


def normalized(external='pix-current', amount=5000):
    payload = deepcopy(OFFICIAL_PAYLOAD)
    payload['id'], payload['amount'] = external, amount
    return normalize_payload(payload)


def prepare(storage, external='pix-current', tenant='demo', amount=5000):
    n = normalized(external, amount)
    p = to_pix(n, tenant)
    claim = storage.claim(tenant, external, n.payload_hash)
    initial = storage.claim_initial(claim, p.payer)
    if initial is not None:
        r = reputation_record(p.payer.document, tenant=tenant)
        storage.complete_initial(initial, r)
    return n, p, claim


def save(storage, external='pix-current', tenant='demo', amount=5000, security=None, receipt=None):
    n, p, claim = prepare(storage, external, tenant, amount)
    with storage.analysis(claim, p) as work:
        c = work.context(p, datetime.now(timezone.utc), Policy())
        if security is not None:
            c = replace(c, security=security)
        dto = work.complete(n, c, Policy(), receipt_id=receipt)
    return n, p, dto


def event(p, dto, state='executed', *, kind='execution', challenge=None, terminal=True,
          event_id=None, version=0, occurred=None):
    at = datetime.fromisoformat(dto.public['metadata']['decision_at'].replace('Z','+00:00'))
    return EventInput(tenant=p.payer.tenant, provider='bank-demo', event_id=event_id or str(uuid4()),
        kind=kind, payer=p.payer, external_id=p.id, analysis_key=dto.public['key'],
        operation_fingerprint=fingerprint(p), state=state, occurred_at=occurred or at,
        source_version=version, challenge_id=challenge, terminal=terminal)


def expire(storage, table, **key):
    # Deliberately expire a live lease in the disposable DB, without sleeping 10 s.
    assert table in ('analysis_claim', 'source_initial_check')
    where = ' AND '.join(f'{name} = :{name}' for name in key)
    with storage.sessions.begin() as s:
        s.execute(text(f'UPDATE {table} SET lease_until=clock_timestamp()-interval \'1 second\' WHERE {where}'), key)


def test_operation_claim_is_reserved_before_data_and_hash_is_permanent(storage):
    n = normalized()
    claim = storage.claim('demo', n.canonical_payload['id'], n.payload_hash)
    with pytest.raises(Busy):
        storage.claim('demo', claim.external_id, n.payload_hash)
    with pytest.raises(HashConflict):
        storage.claim('demo', claim.external_id, 'f'*64)
    assert storage.get('demo', claim.external_id) is None
    with storage.sessions() as s:
        assert s.execute(text('SELECT count(*) FROM pix_transaction')).scalar() == 0
    expire(storage, 'analysis_claim', tenant_id='demo', external_id=claim.external_id)
    takeover = storage.claim('demo', claim.external_id, n.payload_hash)
    assert takeover.fence == claim.fence+1 and takeover.owner != claim.owner
    with pytest.raises(LostClaim), storage.analysis(claim, to_pix(n, 'demo')):
        pass
    with pytest.raises(HashConflict):
        storage.claim('demo', claim.external_id, 'f'*64)


def test_concurrent_claims_have_only_one_owner_and_conflicting_hash_is_not_busy(storage):
    barrier = Barrier(2)
    def claim():
        barrier.wait()
        try:
            return storage.claim('demo','same-id','a'*64)
        except Busy:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _:claim(), range(2)))
    assert sum(r is not None for r in rows) == 1
    with pytest.raises(HashConflict):
        storage.claim('demo','same-id','b'*64)


def test_initial_check_is_owned_separately_and_old_tokens_cannot_complete(storage):
    n=normalized('first'); p=to_pix(n,'demo'); one=storage.claim('demo',p.id,n.payload_hash)
    first=storage.claim_initial(one,p.payer)
    n2=normalized('second'); two=storage.claim('demo','second',n2.payload_hash)
    with pytest.raises(Busy):
        storage.claim_initial(two,p.payer)
    expire(storage,'source_initial_check',tenant_id='demo',branch=p.payer.branch,account=p.payer.account,document=p.payer.document)
    second=storage.claim_initial(two,p.payer)
    assert second.fence == first.fence+1
    with pytest.raises(LostClaim):
        storage.complete_initial(first,reputation_record(p.payer.document))
    missing=replace(reputation_record(p.payer.document),evidence=replace(reputation_record(p.payer.document).evidence,state='unavailable'))
    storage.complete_initial(second,missing)
    assert storage.initial(p.payer) == missing
    n3=normalized('third'); third=storage.claim('demo','third',n3.payload_hash)
    assert storage.claim_initial(third,p.payer) is None


def test_operation_takeover_fences_even_an_unexpired_initial_ticket(storage):
    n=normalized(); p=to_pix(n,'demo'); old=storage.claim('demo',p.id,n.payload_hash)
    initial=storage.claim_initial(old,p.payer)
    expire(storage,'analysis_claim',tenant_id='demo',external_id=p.id)
    storage.claim('demo',p.id,n.payload_hash)
    with pytest.raises(LostClaim):
        storage.complete_initial(initial,reputation_record(p.payer.document))


def test_tenant_scope_and_account_ownership_are_not_interchangeable(storage):
    n,p,claim=prepare(storage)
    other=storage.claim('other',p.id,n.payload_hash)
    assert other.tenant == 'other'
    n2=normalized('another'); c=storage.claim('demo','another',n2.payload_hash)
    with pytest.raises(OwnershipConflict):
        storage.claim_initial(c,replace(p.payer,document='98765432100'))
    with pytest.raises(ValueError):
        storage.claim_initial(other,p.payer)


def test_atomic_analysis_receipt_replay_and_original_are_frozen(storage):
    receipt=storage.receive('request-1','demo','pix-current')
    n,p,claim=prepare(storage)
    with pytest.raises(RuntimeError), storage.analysis(claim,p) as work:
        work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy(),receipt_id=receipt)
        raise RuntimeError('fail after all inserts, before commit')
    assert storage.get('demo',p.id) is None
    with storage.sessions() as s:
        assert s.execute(text('SELECT count(*) FROM pix_transaction')).scalar() == 0
        assert s.execute(text('SELECT http_status FROM request_receipt WHERE id=:id'),{'id':receipt}).scalar() is None
    with storage.analysis(claim,p) as work:
        dto=work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy(),receipt_id=receipt)
    assert storage.get('demo',p.id) == dto
    assert storage.get('other',p.id) is None
    replay=storage.claim('demo',p.id,n.payload_hash)
    assert isinstance(replay,Replay) and replay.dto == dto
    with pytest.raises(HashConflict):
        storage.claim('demo',p.id,normalized(amount=5001).payload_hash)
    with storage.sessions.begin() as s:
        assert s.execute(text('SELECT http_status FROM request_receipt WHERE id=:id'),{'id':receipt}).scalar() == 201
    for sql in ('UPDATE risk_analysis SET score=999', 'DELETE FROM risk_analysis',
                "UPDATE pix_transaction SET amount_cents=1", 'DELETE FROM pix_transaction',
                "UPDATE analysis_claim SET payload_hash=repeat('b',64)"):
        with pytest.raises(DBAPIError), storage.sessions.begin() as s:
            s.execute(text(sql))


def test_completion_requires_initial_claim_and_matching_payload(storage):
    n=normalized(); p=to_pix(n,'demo'); c=storage.claim('demo',p.id,n.payload_hash)
    with pytest.raises(LostClaim), storage.analysis(c,p) as work:
        work.complete(n,Context(p,datetime.now(timezone.utc)),Policy())
    initial=storage.claim_initial(c,p.payer); storage.complete_initial(initial,reputation_record(p.payer.document))
    with pytest.raises(ValueError), storage.analysis(c,p) as work:
        work.complete(normalized(amount=5001),Context(p,datetime.now(timezone.utc)),Policy())


def test_history_roundtrip_has_occurrence_knowledge_and_no_replay_attempt(storage):
    n,p,dto=save(storage)
    storage.ingest(event(p,dto))
    q=replace(p,id='next')
    with storage.read() as repo:
        c=repo.context(q,datetime.now(timezone.utc),Policy())
        assert len(c.history) == 1 and len(c.execution_events) == 1
        assert c.history[0].status == dto.public['status']
        from antifraud.features import build_features
        assert len(build_features(c,Policy()).paid_payee)==1
        assert not evaluate(c).recipient_established  # execution is not mature yet
        before=repo.context(q,datetime.fromisoformat(dto.public['metadata']['decision_at'].replace('Z','+00:00')),Policy())
        assert before.execution_events == ()
        assert repo.context(replace(q,payer=replace(q.payer,tenant='other')),datetime.now(timezone.utc),Policy()).history == ()


def test_cache_preserves_watermark_conflicts_scope_and_unknown_initial(storage):
    clean=reputation_record()
    newer=reputation_record(observed_at=clean.evidence.observed_at+timedelta(minutes=1),
                            available_at=clean.evidence.available_at+timedelta(minutes=1),valid_until=clean.evidence.valid_until-timedelta(minutes=1),fraud_count=2)
    before_cache=datetime.now(timezone.utc)
    with storage.write() as repo:
        repo.put_snapshots((newer,clean))
    with storage.read() as repo:
        assert repo.reputation(clean.key,before_cache) is None
        selected=repo.reputation(clean.key,datetime.now(timezone.utc))
        assert selected.evidence.state == 'stale' and selected.evidence.fraud_count == 2
        assert repo.reputation(replace(clean.key,tenant='other'),datetime.now(timezone.utc)) is None
    conflict=replace(newer,evidence=replace(newer.evidence,fraud_count=0))
    with pytest.raises(RecordConflict), storage.write() as repo:
        repo.put_snapshots((conflict,))
    with storage.write() as repo:
        repo.put_snapshots((replace(newer,evidence=replace(newer.evidence,state='stale')),))
    sec=security_record(device_state='revoked')
    with storage.write() as repo:
        repo.put_snapshots((sec,))
    with storage.read() as repo:
        assert repo.security(sec.scope,sec.provider,datetime.now(timezone.utc)).snapshot.device_state == 'revoked'
        assert repo.security(replace(sec.scope,device_id='other'),sec.provider,datetime.now(timezone.utc)) is None


def test_internal_execution_events_are_idempotent_linked_and_terminal(storage):
    n,p,dto=save(storage)
    row=event(p,dto)
    assert storage.ingest(row) == 'created'
    assert storage.ingest(row) == 'replay'
    with pytest.raises(EventConflict):
        storage.ingest(replace(row,state='failed'))
    with pytest.raises(InvalidEvent):
        storage.ingest(replace(row,event_id='other',state='cancelled',source_version=1))
    for invalid in (replace(row,event_id='bad-fingerprint',operation_fingerprint='a'*64),
                    replace(row,event_id='wrong-key',analysis_key=str(uuid4())),
                    replace(row,event_id='future',occurred_at=datetime.now(timezone.utc)+timedelta(days=1)),
                    replace(row,event_id='other-tenant',tenant='other')):
        with pytest.raises(InvalidEvent):
            storage.ingest(invalid)
    assert storage.get('demo',p.id) == dto


def two_fa(storage):
    from antifraud.context import SecuritySnapshot
    now=datetime.now(timezone.utc)
    security=SecuritySnapshot(now-timedelta(seconds=1),now-timedelta(seconds=1),now+timedelta(minutes=5),'unregistered')
    return save(storage,amount=30000,security=security)


def test_auth_is_separate_and_exact_challenge_binding_releases_exposure(storage):
    n,p,dto=two_fa(storage)
    assert dto.public['status'] == 'two_factor_required'
    storage.bind_challenge('demo','bank-demo',p.id,dto.public['key'],'bank-challenge',fingerprint(p))
    failed=event(p,dto,'failed',kind='auth',challenge='bank-challenge',terminal=False)
    storage.ingest(failed)
    with storage.read() as repo:
        assert len(repo.context(replace(p,id='next'),datetime.now(timezone.utc),Policy()).auth_events) == 1
    passed=replace(failed,event_id='passed',state='passed',terminal=True,source_version=1)
    storage.ingest(passed)
    assert storage.get('demo',p.id) == dto
    with pytest.raises(InvalidEvent):
        storage.ingest(replace(passed,event_id='bad-terminal',state='failed',source_version=2))
    storage.ingest(event(p,dto))  # financial execution is a separate fact
    with storage.read() as repo:
        c=repo.context(replace(p,id='next'),datetime.now(timezone.utc),Policy())
        assert c.history[0].status == 'two_factor_required'
        assert c.history[0].challenge_id == 'bank-challenge'
        assert len(c.execution_events) == 1
        assert c.security is None  # passing 2FA never registers a device


def test_late_nonterminal_failure_does_not_revive_passed_challenge(storage):
    n,p,dto=two_fa(storage)
    storage.bind_challenge('demo','bank-demo',p.id,dto.public['key'],'challenge',fingerprint(p))
    passed=event(p,dto,'passed',kind='auth',challenge='challenge',version=2)
    storage.ingest(passed)
    earlier=replace(passed,event_id='late-failure',state='failed',terminal=False,source_version=1)
    assert storage.ingest(earlier) == 'created'
    with pytest.raises(InvalidEvent):
        storage.ingest(replace(earlier,event_id='new-failure',source_version=3))


def test_bank_links_require_financial_identity_and_cannot_alias_twice(storage):
    n,p,dto=save(storage)
    storage.link_bank_operation('demo','bank-demo','bank-op',p.id,fingerprint(p))
    storage.link_bank_operation('demo','bank-demo','bank-op',p.id,fingerprint(p))
    with pytest.raises(InvalidEvent):
        storage.link_bank_operation('demo','bank-demo','another-op',p.id,fingerprint(p))
    with storage.read() as repo:
        links=repo.bank_links(p.payer)
        assert len(links) == 1 and links[0].local_pix_id == p.id


def test_receipts_are_minimal_and_do_not_create_risk_attempts(storage):
    receipt=storage.receive('bad-request',None)
    storage.finish_receipt(receipt,None,400,'invalid_input')
    with storage.sessions() as s:
        assert s.execute(text('SELECT count(*) FROM pix_transaction')).scalar() == 0
        assert s.execute(text('SELECT category,http_status FROM request_receipt WHERE id=:id'),{'id':receipt}).one() == ('invalid_input',400)


def test_advisory_locks_are_sorted_and_block_same_destination(storage):
    n,p,c=prepare(storage)
    with storage.analysis(c,p) as work:
        ids=work.repository.lock_ids(p)
        assert ids == sorted(set(ids))
        n2=normalized('other'); q=to_pix(n2,'demo'); claim=storage.claim('demo',q.id,n2.payload_hash)
        def competing():
            with storage.analysis(claim,q,lock_timeout_ms=30):
                return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(DBAPIError) as caught:
                pool.submit(competing).result(timeout=3)
        assert caught.value.orig.pgcode == '55P03'


def test_codec_is_explicit_and_roundtrips_provider_contracts():
    r=reputation_record()
    assert unpack(pack(r)) == r
    assert unpack(pack(security_record())) == security_record()
    with pytest.raises(ValueError):
        unpack({'version':1,'value':{'type':'os.system','data':{}}})


def test_stale_local_context_cannot_be_persisted_after_locks(storage):
    n,p,claim=prepare(storage)
    stale=Context(p,datetime.now(timezone.utc)-timedelta(minutes=1))
    with pytest.raises(ValueError), storage.analysis(claim,p) as work:
        work.complete(n,stale,Policy())
    save(storage,'previous')
    with pytest.raises(ValueError), storage.analysis(claim,p) as work:
        now=datetime.now(timezone.utc)
        work.complete(n,Context(p,now),Policy())  # omitted persisted predecessor
    assert storage.get('demo',p.id) is None


def test_terminal_auth_failure_releases_only_exact_challenge_exposure(storage):
    from antifraud.features import build_features
    n,p,dto=two_fa(storage)
    storage.bind_challenge('demo','bank-demo',p.id,dto.public['key'],'challenge',fingerprint(p))
    with storage.read() as r:
        before=r.context(replace(p,id='next'),datetime.now(timezone.utc),Policy())
        assert len(build_features(before,Policy()).exposed)==1
    fail=event(p,dto,'failed',kind='auth',challenge='wrong',terminal=True)
    with pytest.raises(InvalidEvent):
        storage.ingest(fail)
    storage.ingest(replace(fail,challenge_id='challenge'))
    with storage.read() as r:
        after=r.context(replace(p,id='next'),datetime.now(timezone.utc),Policy())
        assert build_features(after,Policy()).exposed==()
        assert storage.get('demo',p.id) == dto


def test_event_race_on_different_operations_same_provider_id_is_conflict(storage):
    _,p,d=save(storage,'one'); _,q,e=save(storage,'two')
    rows=(event(p,d,event_id='global-event'),event(q,e,event_id='global-event'))
    barrier=Barrier(2)
    def run(row):
        barrier.wait()
        try:
            return storage.ingest(row)
        except EventConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run,rows)) == ['conflict','created']


def test_initial_completion_is_immutable_and_cache_scope_is_tenant_bound(storage):
    n,p,c=prepare(storage)
    original=storage.initial(p.payer)
    with pytest.raises(LostClaim):
        from dtos.pix_storage import InitialTicket
        storage.complete_initial(InitialTicket(c,p.payer,uuid4(),1),reputation_record(p.payer.document))
    with pytest.raises(DBAPIError), storage.sessions.begin() as s:
        s.execute(text("UPDATE source_initial_check SET result='{}'::jsonb"))
    assert storage.initial(p.payer)==original


def test_cache_races_accept_one_revision_and_reject_conflicting_content(storage):
    adverse=reputation_record(fraud_count=3)
    clean=replace(adverse,evidence=replace(adverse.evidence,fraud_count=0))
    barrier=Barrier(2)
    def run(row):
        barrier.wait()
        try:
            with storage.write() as r:
                r.put_snapshots((row,))
            return 'stored'
        except RecordConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run,(adverse,clean)))==['conflict','stored']


def test_context_reads_receive_time_even_if_bank_occurrence_is_old(storage):
    _,p,dto=save(storage)
    before=datetime.now(timezone.utc)
    storage.ingest(event(p,dto))
    with storage.read() as r:
        assert r.context(replace(p,id='next'),before,Policy()).execution_events==()
        assert len(r.context(replace(p,id='next'),datetime.now(timezone.utc),Policy()).execution_events)==1
        assert r.reputation(reputation_record().key,before) is None


@pytest.mark.parametrize('legacy',[False,True])
def test_current_migrations_on_empty_or_legacy_base_preserve_data(migration_db,legacy):
    from tests.integration.test_migrations import seed_legacy
    from sqlalchemy import inspect
    if legacy:
        seed_legacy(migration_db)
    assert upgrade(migration_db,adopt_base=legacy)==['0001_base.sql','0002_pix_storage.sql']
    assert upgrade(migration_db,adopt_base=legacy)==[]
    with migration_db.connect() as c:
        names=set(inspect(c).get_table_names())
        assert {'analysis_claim','pix_transaction','risk_analysis','source_initial_check','bank_event',
                'provider_snapshot','bank_challenge','bank_operation_link','request_receipt','pix_account'}<=names
        if legacy:
            assert c.execute(text('SELECT name FROM sample_entity')).scalar()=='Synthetic'


def test_versioned_base_upgrades_incrementally(migration_db,tmp_path):
    from tests.integration.test_migrations import migration_files
    assert upgrade(migration_db,migration_files(tmp_path))==['0001_base.sql']
    assert upgrade(migration_db)==['0002_pix_storage.sql']


def test_query_models_and_database_column_types_agree(storage):
    from models import pix as m
    from sqlalchemy import inspect
    with storage.sessions() as s:
        inspector=inspect(s.connection())
        for table in (m.analysis_claim,m.source_initial,m.account,m.pix,m.analysis,m.receipt,m.challenge,m.link,m.event,m.snapshot):
            columns=inspector.get_columns(table.name)
            assert [c['name'] for c in columns] == list(table.c.keys())
            for col in columns:
                assert str(table.c[col['name']].type.compile(s.bind.dialect))==str(col['type'].compile(s.bind.dialect))


def test_replay_receipt_can_reference_original_analysis_without_new_attempt(storage):
    _,p,dto=save(storage)
    receipt=storage.receive('retry','demo',p.id)
    storage.finish_receipt(receipt,'demo',200,'replay',analysis_key=dto.public['key'])
    with storage.sessions() as s:
        assert s.execute(text('SELECT count(*) FROM risk_analysis')).scalar()==1
        assert str(s.execute(text('SELECT analysis_key FROM request_receipt WHERE id=:id'),{'id':receipt}).scalar())==dto.public['key']
    wrong=storage.receive('other','other',p.id)
    with pytest.raises(ValueError):
        storage.finish_receipt(wrong,'other',200,'replay',analysis_key=dto.public['key'])


def test_event_hash_same_payload_and_equivalent_offsets_is_replay(storage):
    _,p,dto=save(storage)
    row=event(p,dto)
    storage.ingest(row)
    from datetime import timezone
    alternate=replace(row,occurred_at=row.occurred_at.astimezone(timezone(timedelta(hours=-3))))
    assert storage.ingest(alternate)=='replay'


def test_pending_and_expired_challenges_do_not_register_devices(storage):
    n,p,dto=two_fa(storage)
    storage.bind_challenge('demo','bank-demo',p.id,dto.public['key'],'expiring',fingerprint(p))
    expired=event(p,dto,'expired',kind='auth',challenge='expiring')
    storage.ingest(expired)
    with pytest.raises(InvalidEvent):
        storage.ingest(replace(expired,event_id='late-pass',state='passed',source_version=1))
    with storage.read() as r:
        c=r.context(replace(p,id='next'),datetime.now(timezone.utc),Policy())
        assert c.auth_events[0].state=='failed' and c.auth_events[0].terminal
        assert c.security is None


def test_concurrent_initial_checks_only_one_account_owner(storage):
    first=normalized('one'); second=normalized('two'); p=to_pix(first,'demo')
    claims=(storage.claim('demo','one',first.payload_hash),storage.claim('demo','two',second.payload_hash))
    barrier=Barrier(2)
    def claim(c):
        barrier.wait()
        try:
            return storage.claim_initial(c,p.payer)
        except Busy:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(claim,claims))
    assert sum(r is not None for r in results)==1


def test_db_error_rolls_back_both_analysis_and_claim_completion(storage):
    n,p,c=prepare(storage)
    with pytest.raises(DBAPIError), storage.analysis(c,p) as work:
        work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy())
        work.repository.session.execute(text('SELECT 1/0'))
    assert storage.get('demo',p.id) is None
    with pytest.raises(Busy):
        storage.claim('demo',p.id,n.payload_hash)
    with storage.analysis(c,p) as work:
        dto=work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy())
    assert storage.get('demo',p.id)==dto


def test_distinct_payers_same_destination_share_lock(storage):
    n,p,c=prepare(storage)
    other=deepcopy(OFFICIAL_PAYLOAD); other['id']='other-payer'; other['amount']=5000
    other['source_account']['account_number']='99999'
    n2=normalize_payload(other); q=to_pix(n2,'demo'); ticket=storage.claim('demo',q.id,n2.payload_hash)
    initial=storage.claim_initial(ticket,q.payer); storage.complete_initial(initial,reputation_record(q.payer.document))
    with storage.analysis(c,p) as first:
        assert len(set(first.repository.lock_ids(p))&set(first.repository.lock_ids(q)))==1
        def run():
            with storage.analysis(ticket,q,lock_timeout_ms=30):
                return True
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(DBAPIError) as caught:
                pool.submit(run).result(timeout=3)
        assert caught.value.orig.pgcode=='55P03'


def test_real_deferred_commit_failure_rolls_back_completed_analysis(storage):
    n,p,t=prepare(storage)
    with storage.sessions.begin() as s:
        s.execute(text("""CREATE FUNCTION fail_commit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'injected deferred constraint' USING ERRCODE='23514'; END $$"""))
        s.execute(text("""CREATE CONSTRAINT TRIGGER fail_at_commit AFTER INSERT ON risk_analysis
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION fail_commit()"""))
    with pytest.raises(DBAPIError),storage.analysis(t,p) as work:
        work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy())
    assert storage.get('demo',p.id) is None
    with storage.sessions() as s:
        assert s.execute(text("SELECT state FROM analysis_claim")).scalar()=='in_progress'


def test_analysis_writer_does_not_invent_past_knowledge_and_indexes_exist(storage):
    n,p,dto=save(storage)
    from sqlalchemy import inspect
    with storage.sessions() as s:
        indexes=inspect(s.connection()).get_indexes('pix_transaction')
        assert {'ix_pix_payer_time','ix_pix_destination_time'}<={i['name'] for i in indexes}
        assert 'ix_analysis_decision' in {i['name'] for i in inspect(s.connection()).get_indexes('risk_analysis')}
    q=replace(p,id='later',transaction_at=p.transaction_at+timedelta(days=91))
    with storage.read() as r:
        c=r.context(q,datetime.now(timezone.utc)+timedelta(minutes=11),Policy())
        assert c.history==()  # neither 90-day behavior nor 10-min receipt window


def test_invalid_ip_quality_survives_persistence_history_events_and_replay(storage):
    payload=deepcopy(OFFICIAL_PAYLOAD);payload['id']='invalid-ip';payload['source']['ip']='synthetic invalid IP'
    n=normalize_payload(payload);p=to_pix(n,'demo');ticket=storage.claim('demo',p.id,n.payload_hash)
    initial=storage.claim_initial(ticket,p.payer);storage.complete_initial(initial,reputation_record(p.payer.document))
    with storage.analysis(ticket,p) as work:
        dto=work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy())
    assert dto.audit['invalid_ip_raw']=='synthetic invalid IP'
    assert dto.audit['payload']['source']['ip'] is None
    storage.ingest(event(p,dto))
    with storage.read() as r:
        c=r.context(replace(p,id='next'),datetime.now(timezone.utc),Policy())
        assert len(c.history)==1 and c.history[0].pix==p
    assert storage.claim('demo',p.id,n.payload_hash).dto==dto


def test_terminal_execution_cannot_move_its_original_occurrence(storage):
    _,p,dto=save(storage)
    first=event(p,dto)
    storage.ingest(first)
    with pytest.raises(InvalidEvent):
        storage.ingest(replace(first,event_id='different-time',occurred_at=first.occurred_at+timedelta(microseconds=1),source_version=1))


def test_analysis_receipt_identity_and_received_time_are_preserved(storage):
    n,p,t=prepare(storage)
    wrong=storage.receive('wrong','demo','different-operation')
    with pytest.raises(ValueError), storage.analysis(t,p) as work:
        work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy(),receipt_id=wrong)
    receipt=storage.receive('right','demo',p.id,n.payload_hash)
    with storage.analysis(t,p) as work:
        work.complete(n,work.context(p,datetime.now(timezone.utc),Policy()),Policy(),receipt_id=receipt)
    with storage.sessions() as s:
        assert s.execute(text('SELECT p.received_at=r.received_at FROM pix_transaction p JOIN request_receipt r ON r.external_id=p.external_id WHERE r.id=:id'),{'id':receipt}).scalar()


def test_naive_times_are_rejected_before_history_or_cache_reads(storage):
    n,p,_=prepare(storage)
    naive=datetime(2026,10,18)
    with storage.read() as r:
        with pytest.raises(ValueError):
            r.context(p,naive,Policy())
        with pytest.raises(ValueError):
            r.reputation(reputation_record().key,naive)
        s=security_record(p)
        with pytest.raises(ValueError):
            r.security(s.scope,s.provider,naive)


def test_financial_fact_does_not_rewrite_original_rejection(storage):
    from antifraud.context import SecuritySnapshot
    now=datetime.now(timezone.utc)
    sec=SecuritySnapshot(now-timedelta(seconds=1),now-timedelta(seconds=1),now+timedelta(minutes=5),'revoked')
    _,p,dto=save(storage,security=sec)
    assert dto.public['status']=='rejected'
    storage.ingest(event(p,dto))
    assert storage.get('demo',p.id)==dto
    with storage.read() as r:
        assert len(r.context(replace(p,id='next'),datetime.now(timezone.utc),Policy()).execution_events)==1


def test_invalid_event_scalar_types_fail_at_normalized_boundary(storage):
    _,p,dto=save(storage)
    base=event(p,dto)
    for changes in ({'analysis_key':1},{'event_id':'bad\x00id'},{'occurred_at':None},
                    {'operation_fingerprint':'not-a-hash'},{'challenge_id':'\x00'},
                    {'source_version':2**63},{'kind':[]},{'state':[]}):
        with pytest.raises(InvalidEvent):
            replace(base,**changes)


def test_snapshot_batches_reversed_versions_use_one_lock_order(storage):
    from sqlalchemy import event as sa_event
    from threading import local
    from time import sleep
    local_state=local(); barrier=Barrier(2)
    engine=storage.sessions.kw['bind']
    def delay_first_insert(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('INSERT INTO provider_snapshot') and not getattr(local_state,'delayed',False):
            local_state.delayed=True
            sleep(.04)  # yield after real row insertion, not a mocked DB operation
    sa_event.listen(engine,'after_cursor_execute',delay_first_insert)
    first=reputation_record(); second=replace(first,evidence=replace(first.evidence,source_version=1))
    def run(rows):
        with storage.write() as repo:
            barrier.wait()
            repo.put_snapshots(rows)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run,((first,second),(second,first))))
    finally:
        sa_event.remove(engine,'after_cursor_execute',delay_first_insert)


def test_valid_policy_scores_are_not_silently_limited_to_int32(storage):
    from antifraud.context import SecuritySnapshot
    base=Policy()
    rules=tuple((name,(('points',3000000000),) if name=='UNREGISTERED_DEVICE' else args) for name,args in base.rule_parameters)
    policy=replace(base,family_caps=tuple((family,4000000000) if family=='Acesso' else (family,cap) for family,cap in base.family_caps),rule_parameters=rules)
    n,p,t=prepare(storage)
    with storage.analysis(t,p) as work:
        now=work.repository.now()
        sec=SecuritySnapshot(now,now,now+timedelta(minutes=5),'unregistered')
        c=replace(work.context(p,now,policy),security=sec)
        dto=work.complete(n,c,policy)
    assert dto.public['metadata']['score']==3000000010  # +10 recent payee account remains active
    assert storage.get('demo',p.id)==dto
