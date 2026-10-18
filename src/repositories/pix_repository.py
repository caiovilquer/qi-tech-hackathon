"""Tenant-scoped SQL only. Caller owns every transaction and commit."""
from dataclasses import replace
from datetime import timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select, and_, or_, func
from sqlalchemy.dialects.postgresql import insert

from models import pix as m
from antifraud.context import Context, Pix, AccountKey, DestinationKey, HistoricalAnalysis, ExecutionEvent, AuthEvent, supported_time
from antifraud.dto import PixTransactionDTO, _freeze
from providers.contracts import BankRecord, ReputationRecord, BankLink
from providers.selection import select_security, select_reputation
from dtos.pix_storage import LostClaim, OwnershipConflict, RecordConflict
from utils.pix_storage_codec import pack, unpack, digest


def scope(table, **values):
    return and_(*(table.c[k]==v for k,v in values.items()))


def initial_key(payer):
    return dict(tenant_id=payer.tenant,branch=payer.branch,account=payer.account,document=payer.document)


class PixRepository:
    def __init__(self, context):
        self.session=context.db_session

    def now(self):
        return self.session.execute(select(func.clock_timestamp())).scalar_one()

    def ensure_account(self,payer):
        values=initial_key(payer)
        self.session.execute(insert(m.account).values(**values).on_conflict_do_nothing())
        row=self.session.execute(select(m.account).where(scope(m.account,**{k:v for k,v in values.items() if k!='document'}))).mappings().one()
        if row['document']!=payer.document:
            raise OwnershipConflict('Account owner changed; explicit reconciliation required')

    def require_claim(self,ticket):
        row=self.session.execute(select(m.analysis_claim).where(scope(m.analysis_claim,
            tenant_id=ticket.tenant,external_id=ticket.external_id)).with_for_update()).mappings().first()
        if (row is None or row['payload_hash']!=ticket.payload_hash or row['owner_id']!=ticket.owner
                or row['fencing_token']!=ticket.fence or row['state']!='in_progress' or row['lease_until']<=self.now()):
            raise LostClaim('Operation owner/token/lease is no longer valid')
        return row

    @staticmethod
    def lock_ids(pix,extra=()):
        # Actual signed int64 identifiers are sorted, including collisions.
        keys=(('payer',pix.payer.tenant,pix.payer.branch,pix.payer.account),
              ('destination',pix.payer.tenant,pix.destination.participant,pix.destination.branch,pix.destination.account))+tuple(extra)
        return sorted({int.from_bytes(sha256(_freeze(k).encode()).digest()[:8],'big',signed=True) for k in keys})

    def lock(self,pix,extra=()):
        for key in self.lock_ids(pix,extra):
            self.session.execute(select(func.pg_advisory_xact_lock(key)))

    def get(self,tenant,external_id):
        row=self.local(tenant,external_id)
        return self.dto(row) if row else None

    def local(self,tenant,external_id):
        return self.session.execute(select(m.pix,m.analysis.c.key,m.analysis.c.status,m.analysis.c.decision_at,
            m.analysis.c.sequence,m.analysis.c.device_state,m.analysis.c.night_qualifies,m.analysis.c.public_response,
            m.analysis.c.snapshot).join(m.analysis,and_(m.analysis.c.pix_id==m.pix.c.id,m.analysis.c.tenant_id==m.pix.c.tenant_id))
            .where(m.pix.c.tenant_id==tenant,m.pix.c.external_id==external_id)).mappings().first()

    @staticmethod
    def dto(row):
        return PixTransactionDTO(_freeze(row['public_response']),_freeze(row['snapshot']))

    @staticmethod
    def domain_pix(row):
        # Canonical audit payload can contain ip=null after quality normalization;
        # it must not be parsed again as a fresh official HTTP request.
        return Pix(row['external_id'],AccountKey(row['tenant_id'],row['payer_branch'],row['payer_account'],row['payer_document']),
                   row['payee_document'],DestinationKey(row['participant'],row['destination_branch'],row['destination_account']),
                   row['amount_cents'],row['transaction_at'],row['destination_opened_at'],row['device_id'])

    def context(self,pix,at,policy):
        supported_time(at,'history knowledge time')
        at=at.astimezone(timezone.utc)
        t=pix.transaction_at.astimezone(timezone.utc)
        mine=and_(m.pix.c.payer_branch==pix.payer.branch,m.pix.c.payer_account==pix.payer.account,
                  m.pix.c.payer_document==pix.payer.document)
        destination=and_(m.pix.c.participant==pix.destination.participant,m.pix.c.destination_branch==pix.destination.branch,
                         m.pix.c.destination_account==pix.destination.account)
        history=and_(m.pix.c.transaction_at>t-timedelta(days=policy.history_days),m.pix.c.transaction_at<=t,or_(mine,destination))
        receipts=and_(mine,m.analysis.c.decision_at>at-timedelta(minutes=policy.velocity_minutes))
        query=select(m.pix,m.analysis.c.status,m.analysis.c.decision_at,m.analysis.c.sequence,
                     m.analysis.c.device_state,m.analysis.c.night_qualifies,m.challenge.c.challenge_id).join(m.analysis,
            and_(m.analysis.c.tenant_id==m.pix.c.tenant_id,m.analysis.c.pix_id==m.pix.c.id)).outerjoin(m.challenge,
            and_(m.challenge.c.tenant_id==m.pix.c.tenant_id,m.challenge.c.pix_id==m.pix.c.id,m.challenge.c.received_at<=at)).where(
                m.pix.c.tenant_id==pix.payer.tenant,m.analysis.c.decision_at<=at,or_(history,receipts),
                or_(~mine,m.pix.c.external_id!=pix.id)).order_by(m.analysis.c.sequence)
        rows=self.session.execute(query).mappings().all()
        histories=tuple(HistoricalAnalysis(self.domain_pix(r),r['decision_at'],r['sequence'],r['status'],
                          r['challenge_id'],r['device_state'],r['night_qualifies']) for r in rows)
        ids=[r['id'] for r in rows if self.domain_pix(r).payer==pix.payer]
        executions,auth=[],[]
        if ids:
            events=self.session.execute(select(m.event).where(m.event.c.tenant_id==pix.payer.tenant,
                m.event.c.pix_id.in_(ids),m.event.c.occurred_at<=at,m.event.c.received_at<=at)
                .order_by(m.event.c.occurred_at,m.event.c.source_version,m.event.c.provider,m.event.c.event_id)).mappings().all()
            external={r['id']:r['external_id'] for r in rows}
            for e in events:
                event_id=f"{e['provider']}:{e['event_id']}"
                if e['kind']=='execution':
                    executions.append(ExecutionEvent(event_id,pix.payer,external[e['pix_id']],e['state'],e['occurred_at'],e['received_at']))
                else:
                    auth.append(AuthEvent(event_id,pix.payer,external[e['pix_id']],e['challenge_id'],
                        'failed' if e['state']=='expired' else e['state'],e['terminal'],e['occurred_at'],e['received_at']))
        return Context(pix,at,histories,tuple(executions),tuple(auth))

    def initial(self,payer):
        value=self.session.execute(select(m.source_initial.c.result).where(scope(m.source_initial,**initial_key(payer)),
            m.source_initial.c.state=='completed')).scalar_one_or_none()
        return unpack(value) if value else None

    @staticmethod
    def snapshot_key(record):
        if isinstance(record,ReputationRecord):
            return record.key.tenant,digest(pack(record.key)),'reputation',record.evidence
        if isinstance(record,BankRecord):
            return record.scope.payer.tenant,digest(pack((record.scope,record.provider))),'security',record.snapshot
        raise ValueError('Only normalized provider records can be cached')

    def put_snapshots(self,records):
        def order(record):
            tenant,key,kind,value=self.snapshot_key(record)
            return (tenant,key,value.observed_at.isoformat() if value else '',value.source_version if value else 0)
        records=sorted(tuple(records),key=order)
        for record in records:
            tenant,key,kind,value=self.snapshot_key(record)
            state=record.evidence.state if isinstance(record,ReputationRecord) else record.state
            if value is None or state not in ('available','stale'):
                continue  # Unknown initial coverage is durable in source_initial_check, never clean cache.
            body=pack(record)
            fact=pack(replace(value,available_at=value.observed_at))
            if kind=='reputation':
                fact=pack(replace(value,state='available',available_at=value.observed_at))
            identity=dict(tenant_id=tenant,key_hash=key,observed_at=value.observed_at,source_version=value.source_version)
            values=dict(identity,id=uuid4(),kind=kind,available_at=value.available_at,valid_until=value.valid_until,
                        fact_hash=digest((fact,record.provenance if kind=='security' else value.provenance)),record=body)
            self.session.execute(insert(m.snapshot).values(**values).on_conflict_do_nothing())
            previous=self.session.execute(select(m.snapshot).where(scope(m.snapshot,**identity)).with_for_update()).mappings().one()
            if previous['fact_hash']!=values['fact_hash']:
                raise RecordConflict('Conflicting provider source revision')
            old=unpack(previous['record'])
            old_value=old.snapshot if kind=='security' else old.evidence
            available=min(old_value.available_at,value.available_at)
            is_stale=state=='stale' or (old.state if kind=='security' else old.evidence.state)=='stale'
            if kind=='reputation':
                merged=replace(record,evidence=replace(value,available_at=available,state='stale' if is_stale else 'available'))
            else:
                merged=replace(record,snapshot=replace(value,available_at=available),state='stale' if is_stale else 'available')
            self.session.execute(m.snapshot.update().where(m.snapshot.c.id==previous['id']).values(
                available_at=available,record=pack(merged)))

    def _cached(self,tenant,key,at):
        supported_time(at,'cache knowledge time')
        row=self.session.execute(select(m.snapshot.c.record).where(m.snapshot.c.tenant_id==tenant,
            m.snapshot.c.key_hash==key,m.snapshot.c.observed_at<=at,m.snapshot.c.available_at<=at,
            m.snapshot.c.received_at<=at).order_by(m.snapshot.c.observed_at.desc(),m.snapshot.c.source_version.desc()).limit(1)).scalar_one_or_none()
        return unpack(row) if row else None

    def reputation(self,key,at):
        record=self._cached(key.tenant,digest(pack(key)),at)
        return select_reputation(key,(record,),at) if record else None

    def security(self,scope_,provider,at):
        record=self._cached(scope_.payer.tenant,digest(pack((scope_,provider))),at)
        return select_security(scope_,provider,(record,),at) if record else None

    def bank_links(self,payer):
        rows=self.session.execute(select(m.link.c.provider,m.link.c.operation_id,m.pix.c.external_id).join(m.pix,
            and_(m.pix.c.tenant_id==m.link.c.tenant_id,m.pix.c.id==m.link.c.pix_id)).where(
                m.pix.c.tenant_id==payer.tenant,m.pix.c.payer_branch==payer.branch,m.pix.c.payer_account==payer.account,
                m.pix.c.payer_document==payer.document)).all()
        return tuple(BankLink(payer.tenant,r.provider,r.operation_id,r.external_id) for r in rows)
