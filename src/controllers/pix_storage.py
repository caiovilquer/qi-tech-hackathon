"""Internal transaction coordinator. No routes, provider I/O or hidden repo commits."""
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
import re

from sqlalchemy import select, func, and_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from models import pix as m
from repositories.pix_repository import PixRepository, scope, initial_key
from dtos.pix_storage import (ClaimTicket,InitialTicket,Replay,Busy,LostClaim,HashConflict,
    EventInput,EventConflict,InvalidEvent)
from antifraud.adapter import to_pix
from antifraud.contract import normalize_json
from antifraud.dto import PixTransactionDTO
from antifraud.engine import evaluate
from providers.contracts import ReputationRecord, account, ProviderIdentity
from utils.pix_storage_codec import pack, digest, fingerprint

LEASE=timedelta(seconds=10)
LOCK_NOT_AVAILABLE='55P03'  # PostgreSQL lock_timeout


def identity(tenant,external=None,hash_=None):
    if not isinstance(tenant,str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}',tenant) is None:
        raise ValueError('Invalid tenant')
    if external is not None and (not isinstance(external,str) or not external.isprintable() or not 1<=len(external)<=128):
        raise ValueError('Invalid external ID')
    if hash_ is not None and (not isinstance(hash_,str) or re.fullmatch(r'[a-f0-9]{64}',hash_) is None):
        raise ValueError('Invalid canonical hash')


def lock_timeout(error):
    return isinstance(error,DBAPIError) and getattr(error.orig,'pgcode',None)==LOCK_NOT_AVAILABLE


def binding(tenant,provider,external_id,fingerprint_,key=None,*,require_key=False):
    """Validate a bank binding at the boundary. Every rejection is QIT002007."""
    try:
        identity(tenant,external_id)
        ProviderIdentity(provider,'v1')
        if not isinstance(fingerprint_,str) or re.fullmatch(r'[a-f0-9]{64}',fingerprint_) is None:
            raise ValueError('Invalid financial fingerprint')
        if key is None and require_key:
            raise ValueError('Analysis key is required for a bank binding')
        if key is not None and not isinstance(key,str):
            raise ValueError('Analysis key must be a UUID string')
        return str(UUID(key)) if key is not None else None
    except (TypeError,ValueError,AttributeError) as error:
        raise InvalidEvent(str(error)) from error


def bank_reference(value,label):
    if not isinstance(value,str) or not value.strip() or not value.isprintable() or len(value)>128:
        raise InvalidEvent(f'Explicit {label} required')
    return value


class AnalysisWork:
    def __init__(self,repository,ticket,pix):
        self.repository,self.ticket,self.pix=repository,ticket,pix
        self.locked_at=repository.now()

    def now(self):
        """PostgreSQL UTC clock under the held locks; capture decision_at with it."""
        return self.repository.now()

    def context(self,pix,at,policy):
        if pix!=self.pix:
            raise ValueError('Locked operation does not match context')
        return self.repository.context(pix,at,policy)

    @staticmethod
    def same_local_facts(context,current):
        """The decision must use exactly the persisted local facts known at decision_at.

        Linked bank executions are local facts once ingested; the coordinator
        persists them before the final transaction instead of carrying a second,
        unverifiable copy of the same execution in the Context.
        """
        if Counter(context.history)!=Counter(current.history) or Counter(context.auth_events)!=Counter(current.auth_events):
            raise ValueError('Local history/auth events changed; rebuild context under the held locks')
        if Counter(context.execution_events)==Counter(current.execution_events):
            return
        if set(context.execution_events)-set(current.execution_events):
            raise ValueError('Execution events are not persisted locally; ingest linked bank executions before the final transaction')
        raise ValueError('Local execution events changed; rebuild context under the held locks')

    def complete(self,normalized,context,policy,*,receipt_id=None,raw_body=None):
        r,t=self.repository,self.ticket
        r.require_claim(t)
        if (context.pix!=self.pix or to_pix(normalized,t.tenant)!=self.pix
                or normalized.payload_hash!=t.payload_hash or self.pix.id!=t.external_id):
            raise ValueError('Payload/context does not match permanent reservation')
        if r.initial(self.pix.payer) is None:
            raise LostClaim('Source initial check is not durably completed')
        now=r.now()
        if context.decision_at<self.locked_at:
            raise ValueError('decision_at precedes the account locks; capture it with work.now() after locking')
        if context.decision_at>now:
            raise ValueError('decision_at is ahead of the PostgreSQL clock; capture it with work.now()')
        self.same_local_facts(context,r.context(self.pix,context.decision_at,policy))
        if context.security is not None and not context.security.valid(context.decision_at):
            raise ValueError('Known expired bank context must be refreshed before persistence')
        if raw_body is not None:
            if not isinstance(raw_body,bytes) or len(raw_body)>65536 or normalize_json(raw_body).payload_hash!=t.payload_hash:
                raise ValueError('Raw official body must match canonical hash and size limit')
        decision=evaluate(context,policy)
        receipt_row=None
        if receipt_id is not None:
            receipt_row=r.session.execute(select(m.receipt).where(scope(m.receipt,id=receipt_id,tenant_id=t.tenant)).with_for_update()).mappings().first()
            if (receipt_row is None or receipt_row['http_status'] is not None
                    or receipt_row['external_id'] not in (None,t.external_id)
                    or receipt_row['payload_hash'] not in (None,t.payload_hash)):
                raise ValueError('Receipt identity does not match the original request')
        key,pix_id=uuid4(),uuid4()
        dto=PixTransactionDTO.from_decision(str(key),normalized,context,policy,decision)
        p=self.pix; data=normalized.canonical_payload
        values=dict(id=pix_id,tenant_id=t.tenant,external_id=p.id,payload_hash=t.payload_hash,payload=data,
            raw_body=raw_body,received_at=receipt_row['received_at'] if receipt_row else now,amount_cents=p.amount_cents,payer_branch=p.payer.branch,payer_account=p.payer.account,
            payer_document=p.payer.document,payee_document=p.payee_document,participant=p.destination.participant,
            destination_branch=p.destination.branch,destination_account=p.destination.account,transaction_at=p.transaction_at,
            source_opened_at=data['source_account']['opening_date'],destination_opened_at=p.destination_opened_at,
            device_id=p.device_id,channel=data['source']['channel'],ip=data['source']['ip'],
            geolocation=data['source'].get('geolocation'))
        r.session.execute(m.pix.insert().values(**values))
        r.session.execute(m.analysis.insert().values(key=key,tenant_id=t.tenant,pix_id=pix_id,status=decision.status,
            decision_at=context.decision_at,policy_version=policy.version,score=decision.score,
            device_state=decision.device_state,night_qualifies=decision.night_qualifies,
            public_response=dto.public,snapshot=dto.audit))
        r.session.execute(m.analysis_claim.update().where(scope(m.analysis_claim,tenant_id=t.tenant,
            external_id=t.external_id)).values(state='completed',analysis_key=key))
        if receipt_id is not None:
            result=r.session.execute(m.receipt.update().where(scope(m.receipt,id=receipt_id,tenant_id=t.tenant),
                m.receipt.c.http_status.is_(None)).values(http_status=201,category='analysis',analysis_key=key,
                                                      external_id=t.external_id,payload_hash=t.payload_hash))
            if result.rowcount!=1:
                raise ValueError('Receipt missing, finalized or outside tenant')
        r.session.flush()
        return dto  # Internal value; the coordinator context must commit before HTTP success.


class PixStorage:
    def __init__(self,sessions):
        self.sessions=sessions

    @staticmethod
    def _configure(session,lock_timeout_ms):
        # Always override a server default of SERIALIZABLE/REPEATABLE READ.
        session.connection(execution_options={'isolation_level':'READ COMMITTED'})
        session.execute(select(func.set_config('search_path','public',True)))
        session.execute(select(func.set_config('lock_timeout',f'{lock_timeout_ms}ms',True)))
        session.execute(select(func.set_config('statement_timeout','3000ms',True)))

    @staticmethod
    def _timeout(lock_timeout_ms):
        if type(lock_timeout_ms) is not int or not 1<=lock_timeout_ms<=1000:
            raise ValueError('Lock timeout must be 1..1000 milliseconds')
        return lock_timeout_ms

    @contextmanager
    def write(self,*,lock_timeout_ms=1000):
        timeout=self._timeout(lock_timeout_ms)
        with self.sessions() as session:
            try:
                self._configure(session,timeout)
                yield PixRepository(SimpleNamespace(db_session=session))
                session.flush()
                session.commit()  # Failures, including commit failures, propagate after rollback.
            except BaseException:
                session.rollback()
                raise

    @contextmanager
    def read(self):
        with self.sessions() as session:
            self._configure(session,1000)
            yield PixRepository(SimpleNamespace(db_session=session))
            # close() rolls back a read transaction; no commit and no detached ORM state.

    @contextmanager
    def coordination(self,label,*,lock_timeout_ms=1000):
        """Short reservation transaction: waiting on a reservation row is a live owner, not a technical failure."""
        try:
            with self.write(lock_timeout_ms=lock_timeout_ms) as r:
                yield r
        except DBAPIError as error:
            if lock_timeout(error):
                raise Busy(f'{label} is held by a live owner; retry within the request deadline') from error
            raise

    def claim(self,tenant,external_id,payload_hash,*,lock_timeout_ms=1000):
        identity(tenant,external_id,payload_hash)
        with self.read() as r:
            # No row lock: a conflicting hash or a finished analysis answers at once,
            # even while the owner is finalizing and holds the reservation row.
            row=r.session.execute(select(m.analysis_claim.c.payload_hash,m.analysis_claim.c.state).where(
                scope(m.analysis_claim,tenant_id=tenant,external_id=external_id))).mappings().first()
            if row is not None:
                if row['payload_hash']!=payload_hash:
                    raise HashConflict('Tenant/ID permanently reserved with another payload')
                if row['state']=='completed':
                    return Replay(r.get(tenant,external_id))
        owner=uuid4()
        with self.coordination('Operation reservation',lock_timeout_ms=lock_timeout_ms) as r:
            values=dict(tenant_id=tenant,external_id=external_id,payload_hash=payload_hash,owner_id=owner,
                        fencing_token=1,lease_until=func.clock_timestamp()+LEASE,state='in_progress')
            r.session.execute(insert(m.analysis_claim).values(**values).on_conflict_do_nothing())
            row=r.session.execute(select(m.analysis_claim).where(scope(m.analysis_claim,tenant_id=tenant,
                external_id=external_id)).with_for_update()).mappings().one()
            if row['payload_hash']!=payload_hash:
                raise HashConflict('Tenant/ID permanently reserved with another payload')
            if row['state']=='completed':
                return Replay(r.get(tenant,external_id))
            fence,lease=row['fencing_token'],row['lease_until']
            if row['owner_id']!=owner:
                if lease>r.now():
                    raise Busy('Operation already has an active owner')
                fence+=1
                lease=r.session.execute(m.analysis_claim.update().where(scope(m.analysis_claim,tenant_id=tenant,
                    external_id=external_id)).values(owner_id=owner,fencing_token=fence,
                        lease_until=func.clock_timestamp()+LEASE).returning(m.analysis_claim.c.lease_until)).scalar_one()
            return ClaimTicket(tenant,external_id,payload_hash,owner,fence,lease)

    def claim_initial(self,ticket,payer,*,lock_timeout_ms=1000):
        account(payer)
        if payer.tenant!=ticket.tenant:
            raise ValueError('Initial check outside operation tenant')
        owner=uuid4(); key=initial_key(payer)
        with self.coordination('Source initial check',lock_timeout_ms=lock_timeout_ms) as r:
            r.require_claim(ticket)
            r.ensure_account(payer)
            r.session.execute(insert(m.source_initial).values(**key,claim_external_id=ticket.external_id,
                owner_id=owner,fencing_token=1,lease_until=func.clock_timestamp()+LEASE,
                state='in_progress').on_conflict_do_nothing())
            row=r.session.execute(select(m.source_initial).where(scope(m.source_initial,**key)).with_for_update()).mappings().one()
            if row['state']=='completed':
                return None
            fence,lease=row['fencing_token'],row['lease_until']
            if row['owner_id']!=owner:
                if lease>r.now():
                    raise Busy('Source initial lookup has another live owner')
                fence+=1
                lease=r.session.execute(m.source_initial.update().where(scope(m.source_initial,**key)).values(
                    owner_id=owner,fencing_token=fence,claim_external_id=ticket.external_id,
                    lease_until=func.clock_timestamp()+LEASE).returning(m.source_initial.c.lease_until)).scalar_one()
            return InitialTicket(ticket,payer,owner,fence,lease)

    def complete_initial(self,ticket,result,*,lock_timeout_ms=1000):
        if (not isinstance(result,ReputationRecord) or result.key.tenant!=ticket.payer.tenant
                or result.key.document!=ticket.payer.document):
            raise ValueError('Initial result must match exact source subject/tenant')
        with self.coordination('Source initial check',lock_timeout_ms=lock_timeout_ms) as r:
            r.require_claim(ticket.claim)
            row=r.session.execute(select(m.source_initial).where(scope(m.source_initial,
                **initial_key(ticket.payer))).with_for_update()).mappings().first()
            if (row is None or row['state']!='in_progress' or row['owner_id']!=ticket.owner
                    or row['fencing_token']!=ticket.fence or row['claim_external_id']!=ticket.claim.external_id
                    or row['lease_until']<=r.now()):
                raise LostClaim('Initial check owner/token/lease is no longer valid')
            if result.evidence.available_at>r.now():
                raise ValueError('Cannot complete initial check with unknown future coverage')
            # A conflicting cached revision aborts the whole completion: the reservation
            # stays with its owner for a corrected retry or expires for takeover.
            r.put_snapshots((result,))
            r.session.execute(m.source_initial.update().where(scope(m.source_initial,**initial_key(ticket.payer))).values(
                state='completed',result=pack(result),completed_at=func.clock_timestamp()))

    def initial(self,payer):
        with self.read() as r:
            return r.initial(payer)

    @contextmanager
    def analysis(self,ticket,pix,*,lock_timeout_ms=1000):
        if pix.payer.tenant!=ticket.tenant or pix.id!=ticket.external_id:
            raise ValueError('Operation does not match claim tenant/ID')
        with self.write(lock_timeout_ms=lock_timeout_ms) as r:
            r.lock(pix)
            r.require_claim(ticket)
            r.ensure_account(pix.payer)
            yield AnalysisWork(r,ticket,pix)

    def get(self,tenant,external_id):
        identity(tenant,external_id)
        with self.read() as r:
            return r.get(tenant,external_id)

    def receive(self,request_id,tenant=None,external_id=None,payload_hash=None):
        if not isinstance(request_id,str) or not 1<=len(request_id)<=128:
            raise ValueError('Invalid request ID')
        identity(tenant if tenant is not None else '_receipt',external_id,payload_hash)
        key=uuid4()
        with self.write() as r:
            r.session.execute(m.receipt.insert().values(id=key,request_id=request_id,tenant_id=tenant,
                external_id=external_id,payload_hash=payload_hash))
        return key

    def finish_receipt(self,key,tenant,status,category,*,analysis_key=None):
        if type(status) is not int or not 100<=status<=599 or not isinstance(category,str) or not 1<=len(category)<=64:
            raise ValueError('Invalid receipt outcome')
        with self.write() as r:
            if analysis_key is not None:
                analysis_key=UUID(str(analysis_key))
                exists=r.session.execute(select(m.analysis.c.key).where(scope(m.analysis,tenant_id=tenant,key=analysis_key))).scalar_one_or_none()
                if exists is None:
                    raise ValueError('Receipt analysis outside tenant or unknown')
            result=r.session.execute(m.receipt.update().where(scope(m.receipt,id=key,tenant_id=tenant),
                m.receipt.c.http_status.is_(None)).values(http_status=status,category=category,analysis_key=analysis_key))
            if result.rowcount!=1:
                raise ValueError('Receipt missing, finalized or outside tenant')

    def _linked(self,r,tenant,external_id,key=None,fp=None):
        row=r.local(tenant,external_id)
        if row is None or key is not None and str(row['key'])!=str(key):
            raise InvalidEvent('Unknown operation/analysis binding')
        pix=r.domain_pix(row)
        if fp is not None and fingerprint(pix)!=fp:
            raise InvalidEvent('Financial fingerprint mismatch')
        return row,pix

    def bind_challenge(self,tenant,provider,external_id,key,challenge_id,operation_fingerprint):
        key=binding(tenant,provider,external_id,operation_fingerprint,key,require_key=True)
        challenge_id=bank_reference(challenge_id,'bank challenge')
        with self.write() as r:
            row,pix=self._linked(r,tenant,external_id,key,operation_fingerprint)
            r.lock(pix,(('challenge',tenant,provider,challenge_id),))
            if row['status']!='two_factor_required':
                raise InvalidEvent('Challenge only binds an original 2FA analysis')
            old=r.session.execute(select(m.challenge).where(m.challenge.c.tenant_id==tenant,
                (m.challenge.c.pix_id==row['id'])|and_(m.challenge.c.provider==provider,m.challenge.c.challenge_id==challenge_id))).mappings().all()
            if old:
                if len(old)==1 and old[0]['pix_id']==row['id'] and old[0]['provider']==provider and old[0]['challenge_id']==challenge_id:
                    return 'replay'
                raise InvalidEvent('Challenge already linked to another operation')
            r.session.execute(m.challenge.insert().values(tenant_id=tenant,provider=provider,challenge_id=challenge_id,
                pix_id=row['id'],analysis_key=row['key'],operation_fingerprint=operation_fingerprint))
            return 'created'

    def link_bank_operation(self,tenant,provider,operation_id,external_id,operation_fingerprint):
        binding(tenant,provider,external_id,operation_fingerprint)
        operation_id=bank_reference(operation_id,'bank operation ID')
        with self.write() as r:
            row,pix=self._linked(r,tenant,external_id,fp=operation_fingerprint)
            r.lock(pix,(('bank-operation',tenant,provider,operation_id),))
            existing=r.session.execute(select(m.link).where(m.link.c.tenant_id==tenant,m.link.c.provider==provider,
                (m.link.c.pix_id==row['id'])|(m.link.c.operation_id==operation_id))).mappings().all()
            if existing:
                if len(existing)==1 and existing[0]['pix_id']==row['id'] and existing[0]['operation_id']==operation_id:
                    return 'replay'
                raise InvalidEvent('Bank operation/local binding is already in use')
            r.session.execute(m.link.insert().values(tenant_id=tenant,provider=provider,operation_id=operation_id,
                pix_id=row['id'],operation_fingerprint=operation_fingerprint))
            return 'created'

    def ingest(self,value):
        if not isinstance(value,EventInput) or value.tenant!=value.payer.tenant:
            raise InvalidEvent('Bank fact must match tenant/account')
        payload=asdict(value)
        payload['payer']=pack(value.payer)
        payload['occurred_at']=value.occurred_at.astimezone(timezone.utc).isoformat()
        hash_=digest(payload)
        with self.write() as r:
            row,pix=self._linked(r,value.tenant,value.external_id)
            r.lock(pix,(('bank-event',value.tenant,value.provider,value.event_id),))
            old=r.session.execute(select(m.event.c.payload_hash).where(scope(m.event,tenant_id=value.tenant,
                provider=value.provider,event_id=value.event_id))).scalar_one_or_none()
            if old is not None:
                if old!=hash_:
                    raise EventConflict('Accepted event ID has different content')
                return 'replay'
            if (pix.payer!=value.payer or str(row['key'])!=value.analysis_key or fingerprint(pix)!=value.operation_fingerprint
                    or value.occurred_at>r.now() or value.occurred_at<pix.transaction_at):
                raise InvalidEvent('Invalid bank fact scope, chronology or financial binding')
            previous=r.session.execute(select(m.event).where(scope(m.event,tenant_id=value.tenant,pix_id=row['id'],
                kind=value.kind))).mappings().all()
            if value.kind=='execution':
                if value.state not in ('executed','failed','cancelled') or not value.terminal or value.challenge_id is not None:
                    raise InvalidEvent('Invalid terminal execution state')
                if any(e['state']!=value.state or e['occurred_at']!=value.occurred_at for e in previous):
                    raise InvalidEvent('Execution already has another terminal result')
            elif value.kind=='auth':
                if value.state not in ('passed','failed','expired') or value.state!='failed' and not value.terminal:
                    raise InvalidEvent('Invalid authentication result')
                binding_row=r.session.execute(select(m.challenge).where(scope(m.challenge,tenant_id=value.tenant,
                    provider=value.provider,challenge_id=value.challenge_id,pix_id=row['id']))).mappings().first()
                if binding_row is None or value.occurred_at<row['decision_at']:
                    raise InvalidEvent('Authentication requires the exact bound challenge')
                order=(value.occurred_at,value.source_version)
                for e in previous:
                    if e['provider']!=value.provider or e['challenge_id']!=value.challenge_id:
                        continue
                    old_order=(e['occurred_at'],e['source_version'])
                    if e['terminal'] and (value.terminal and e['state']!=value.state or not value.terminal and order>=old_order):
                        raise InvalidEvent('Cannot reopen or change a terminal challenge')
                    if value.terminal and not e['terminal'] and old_order>=order:
                        raise InvalidEvent('Source order contradicts terminal challenge')
            else:
                raise InvalidEvent('Unsupported event type')
            for e in previous:
                if (e['provider']==value.provider and e['occurred_at']==value.occurred_at
                        and e['source_version']==value.source_version
                        and (e['state'],e['terminal'])!=(value.state,value.terminal)):
                    raise InvalidEvent('Conflicting source revision')
            r.session.execute(m.event.insert().values(id=uuid4(),tenant_id=value.tenant,provider=value.provider,
                event_id=value.event_id,payload_hash=hash_,pix_id=row['id'],analysis_key=row['key'],
                operation_fingerprint=value.operation_fingerprint,kind=value.kind,state=value.state,terminal=value.terminal,
                challenge_id=value.challenge_id,occurred_at=value.occurred_at,source_version=value.source_version,payload=payload))
            return 'created'
