"""Tenant-scoped SQL for the Pix tables. The caller owns every transaction and commit."""
from dataclasses import replace
from datetime import timedelta, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select, and_, or_, func
from sqlalchemy.dialects.postgresql import insert

from models import pix as tables
from antifraud.context import (Context, Pix, AccountKey, DestinationKey, HistoricalAnalysis, ExecutionEvent, AuthEvent,
                               supported_time)
from antifraud.dto import PixTransactionDTO, _freeze
from antifraud.features import FAN_IN_WINDOW
from providers.contracts import BankRecord, ReputationRecord, BankLink
from providers.selection import select_security, select_reputation
from dtos.pix_storage import LostClaim, OwnershipConflict, RecordConflict
from utils.pix_storage_codec import pack, unpack, digest


def match_columns(table, **values):
    return and_(*(table.c[column] == value for column, value in values.items()))


def initial_check_key(payer):
    return dict(tenant_id=payer.tenant, branch=payer.branch, account=payer.account, document=payer.document)


class PixRepository:
    def __init__(self, context):
        self.session = context.db_session

    def now(self):
        return self.session.execute(select(func.clock_timestamp())).scalar_one()

    # Accounts, operation claims and advisory locks

    def ensure_account(self, payer):
        values = initial_check_key(payer)
        self.session.execute(insert(tables.account).values(**values).on_conflict_do_nothing())
        account_columns = {column: value for column, value in values.items() if column != 'document'}
        row = self.session.execute(select(tables.account).where(match_columns(tables.account, **account_columns))).mappings().one()
        if row['document'] != payer.document:
            raise OwnershipConflict('Account owner changed; explicit reconciliation required')

    def require_claim(self, ticket):
        row = self.session.execute(select(tables.analysis_claim).where(match_columns(tables.analysis_claim,
            tenant_id=ticket.tenant, external_id=ticket.external_id)).with_for_update()).mappings().first()
        if (row is None or row['payload_hash'] != ticket.payload_hash or row['owner_id'] != ticket.owner
                or row['fencing_token'] != ticket.fence or row['state'] != 'in_progress' or row['lease_until'] <= self.now()):
            raise LostClaim('Operation owner/token/lease is no longer valid')
        return row

    def claim_state(self, tenant, external_id):
        """Hash and state of an operation reservation, read without locking the row."""
        return self.session.execute(select(tables.analysis_claim.c.payload_hash, tables.analysis_claim.c.state).where(
            match_columns(tables.analysis_claim, tenant_id=tenant, external_id=external_id))).mappings().first()

    def reserve_claim(self, tenant, external_id, payload_hash, owner, lease):
        """Inserts the reservation if absent and returns the locked row, whoever owns it."""
        self.session.execute(insert(tables.analysis_claim).values(tenant_id=tenant, external_id=external_id,
            payload_hash=payload_hash, owner_id=owner, fencing_token=1, lease_until=func.clock_timestamp() + lease,
            state='in_progress').on_conflict_do_nothing())
        return self.session.execute(select(tables.analysis_claim).where(match_columns(tables.analysis_claim,
            tenant_id=tenant, external_id=external_id)).with_for_update()).mappings().one()

    def take_over_claim(self, tenant, external_id, owner, fence, lease):
        return self.session.execute(tables.analysis_claim.update().where(match_columns(tables.analysis_claim,
            tenant_id=tenant, external_id=external_id)).values(owner_id=owner, fencing_token=fence,
            lease_until=func.clock_timestamp() + lease).returning(tables.analysis_claim.c.lease_until)).scalar_one()

    def complete_claim(self, tenant, external_id, analysis_key):
        self.session.execute(tables.analysis_claim.update().where(match_columns(tables.analysis_claim,
            tenant_id=tenant, external_id=external_id)).values(state='completed', analysis_key=analysis_key))

    def release_claim(self, ticket):
        """Expires this owner's lease only; returns how many reservations were released."""
        return self.session.execute(tables.analysis_claim.update().where(match_columns(tables.analysis_claim,
            tenant_id=ticket.tenant, external_id=ticket.external_id, payload_hash=ticket.payload_hash,
            owner_id=ticket.owner, fencing_token=ticket.fence, state='in_progress'))
            .values(lease_until=func.clock_timestamp())).rowcount

    @staticmethod
    def lock_ids(pix, extra=()):
        # Signed int64 advisory keys, sorted so every transaction acquires them in the same order.
        keys = (('payer', pix.payer.tenant, pix.payer.branch, pix.payer.account),
                ('destination', pix.payer.tenant, pix.destination.participant, pix.destination.branch,
                 pix.destination.account)) + tuple(extra)
        return sorted({int.from_bytes(sha256(_freeze(key).encode()).digest()[:8], 'big', signed=True) for key in keys})

    def lock(self, pix, extra=()):
        for key in self.lock_ids(pix, extra):
            self.session.execute(select(func.pg_advisory_xact_lock(key)))

    # Source initial check

    def reserve_initial(self, payer, external_id, owner, lease):
        key = initial_check_key(payer)
        self.session.execute(insert(tables.source_initial).values(**key, claim_external_id=external_id,
            owner_id=owner, fencing_token=1, lease_until=func.clock_timestamp() + lease,
            state='in_progress').on_conflict_do_nothing())
        return self.session.execute(select(tables.source_initial).where(match_columns(tables.source_initial, **key))
                                    .with_for_update()).mappings().one()

    def take_over_initial(self, payer, external_id, owner, fence, lease):
        return self.session.execute(tables.source_initial.update().where(match_columns(tables.source_initial,
            **initial_check_key(payer))).values(owner_id=owner, fencing_token=fence, claim_external_id=external_id,
            lease_until=func.clock_timestamp() + lease).returning(tables.source_initial.c.lease_until)).scalar_one()

    def initial_for_update(self, payer):
        return self.session.execute(select(tables.source_initial).where(match_columns(tables.source_initial,
            **initial_check_key(payer))).with_for_update()).mappings().first()

    def complete_initial(self, payer, result):
        self.session.execute(tables.source_initial.update().where(match_columns(tables.source_initial,
            **initial_check_key(payer))).values(state='completed', result=pack(result), completed_at=func.clock_timestamp()))

    def release_initial(self, initial, external_id):
        self.session.execute(tables.source_initial.update().where(match_columns(tables.source_initial,
            **initial_check_key(initial.payer), claim_external_id=external_id, owner_id=initial.owner,
            fencing_token=initial.fence, state='in_progress')).values(lease_until=func.clock_timestamp()))

    def initial(self, payer):
        value = self.session.execute(select(tables.source_initial.c.result).where(
            match_columns(tables.source_initial, **initial_check_key(payer)),
            tables.source_initial.c.state == 'completed')).scalar_one_or_none()
        return unpack(value) if value else None

    # Pix and analyses

    def get(self, tenant, external_id):
        row = self.analysis_row(tenant, external_id)
        return self.dto(row) if row else None

    def analysis_row(self, tenant, external_id):
        """Pix joined with its completed analysis; a Pix row alone is never an analysis."""
        return self.session.execute(select(tables.pix, tables.analysis.c.key, tables.analysis.c.status,
            tables.analysis.c.decision_at, tables.analysis.c.sequence, tables.analysis.c.device_state,
            tables.analysis.c.night_qualifies, tables.analysis.c.public_response, tables.analysis.c.snapshot)
            .join(tables.analysis, and_(tables.analysis.c.pix_id == tables.pix.c.id,
                                        tables.analysis.c.tenant_id == tables.pix.c.tenant_id))
            .where(tables.pix.c.tenant_id == tenant, tables.pix.c.external_id == external_id)).mappings().first()

    @staticmethod
    def dto(row):
        return PixTransactionDTO(_freeze(row['public_response']), _freeze(row['snapshot']))

    @staticmethod
    def domain_pix(row):
        # The canonical audit payload may hold ip=null after quality normalization; it is never re-parsed as HTTP input.
        return Pix(row['external_id'],
                   AccountKey(row['tenant_id'], row['payer_branch'], row['payer_account'], row['payer_document']),
                   row['payee_document'],
                   DestinationKey(row['participant'], row['destination_branch'], row['destination_account']),
                   row['amount_cents'], row['transaction_at'], row['destination_opened_at'], row['device_id'])

    def analysis_exists(self, tenant, analysis_key):
        return self.session.execute(select(tables.analysis.c.key).where(match_columns(tables.analysis,
            tenant_id=tenant, key=analysis_key))).scalar_one_or_none() is not None

    def record_analysis(self, pix_values, analysis_values):
        self.session.execute(tables.pix.insert().values(**pix_values))
        self.session.execute(tables.analysis.insert().values(**analysis_values))

    def context(self, pix, at, policy):
        """Local facts known at `at`: the payer's behavioural window plus the destination fan-in window.

        Rows of other payers only feed the 24 h MULE_FAN_IN aggregate, so they are not loaded for the
        whole behavioural window. Events carry occurrence and local receipt; both must be known at `at`.
        """
        supported_time(at, 'history knowledge time')
        at = at.astimezone(timezone.utc)
        transaction_at = pix.transaction_at.astimezone(timezone.utc)
        same_payer = and_(tables.pix.c.payer_branch == pix.payer.branch, tables.pix.c.payer_account == pix.payer.account,
                          tables.pix.c.payer_document == pix.payer.document)
        same_destination = and_(tables.pix.c.participant == pix.destination.participant,
                                tables.pix.c.destination_branch == pix.destination.branch,
                                tables.pix.c.destination_account == pix.destination.account)
        in_window = and_(tables.pix.c.transaction_at <= transaction_at,
                         or_(and_(same_payer, tables.pix.c.transaction_at > transaction_at - timedelta(days=policy.history_days)),
                             and_(same_destination, tables.pix.c.transaction_at > transaction_at - FAN_IN_WINDOW)))
        recent_receipts = and_(same_payer, tables.analysis.c.decision_at > at - timedelta(minutes=policy.velocity_minutes))
        # Only the columns of a domain Pix: payload/raw_body JSONB would be decoded for every history row.
        pix_columns = [tables.pix.c[name] for name in ('id', 'tenant_id', 'external_id', 'payer_branch', 'payer_account',
            'payer_document', 'payee_document', 'participant', 'destination_branch', 'destination_account',
            'amount_cents', 'transaction_at', 'destination_opened_at', 'device_id')]
        query = select(*pix_columns, tables.analysis.c.status, tables.analysis.c.decision_at, tables.analysis.c.sequence,
                       tables.analysis.c.device_state, tables.analysis.c.night_qualifies, tables.challenge.c.challenge_id).join(
            tables.analysis, and_(tables.analysis.c.tenant_id == tables.pix.c.tenant_id,
                                  tables.analysis.c.pix_id == tables.pix.c.id)).outerjoin(
            tables.challenge, and_(tables.challenge.c.tenant_id == tables.pix.c.tenant_id,
                                   tables.challenge.c.pix_id == tables.pix.c.id, tables.challenge.c.received_at <= at)).where(
            tables.pix.c.tenant_id == pix.payer.tenant, tables.analysis.c.decision_at <= at, or_(in_window, recent_receipts),
            or_(~same_payer, tables.pix.c.external_id != pix.id)).order_by(tables.analysis.c.sequence)
        rows = self.session.execute(query).mappings().all()
        histories = tuple(HistoricalAnalysis(self.domain_pix(row), row['decision_at'], row['sequence'], row['status'],
                                             row['challenge_id'], row['device_state'], row['night_qualifies']) for row in rows)
        payer_row_ids = [row['id'] for row, history in zip(rows, histories) if history.pix.payer == pix.payer]
        executions, authentications = [], []
        if payer_row_ids:
            events = self.session.execute(select(tables.event).where(tables.event.c.tenant_id == pix.payer.tenant,
                tables.event.c.pix_id.in_(payer_row_ids), tables.event.c.occurred_at <= at, tables.event.c.received_at <= at)
                .order_by(tables.event.c.occurred_at, tables.event.c.source_version, tables.event.c.provider,
                          tables.event.c.event_id)).mappings().all()
            external_ids = {row['id']: row['external_id'] for row in rows}
            for event in events:
                event_id = f"{event['provider']}:{event['event_id']}"
                if event['kind'] == 'execution':
                    executions.append(ExecutionEvent(event_id, pix.payer, external_ids[event['pix_id']], event['state'],
                                                     event['occurred_at'], event['received_at']))
                else:
                    authentications.append(AuthEvent(event_id, pix.payer, external_ids[event['pix_id']], event['challenge_id'],
                        'failed' if event['state'] == 'expired' else event['state'], event['terminal'],
                        event['occurred_at'], event['received_at']))
        return Context(pix, at, histories, tuple(executions), tuple(authentications))

    # Provider snapshots (versioned cache)

    @staticmethod
    def snapshot_key(record):
        if isinstance(record, ReputationRecord):
            return record.key.tenant, digest(pack(record.key)), 'reputation', record.evidence
        if isinstance(record, BankRecord):
            return record.scope.payer.tenant, digest(pack((record.scope, record.provider))), 'security', record.snapshot
        raise ValueError('Only normalized provider records can be cached')

    def put_snapshots(self, records):
        def order(record):
            tenant, key, kind, value = self.snapshot_key(record)
            return (tenant, key, value.observed_at.isoformat() if value else '', value.source_version if value else 0)
        for record in sorted(tuple(records), key=order):
            tenant, key, kind, value = self.snapshot_key(record)
            state = record.evidence.state if isinstance(record, ReputationRecord) else record.state
            if value is None or state not in ('available', 'stale'):
                continue  # Unknown coverage lives in source_initial_check, never in the cache as clean data.
            body = pack(record)
            fact = pack(replace(value, available_at=value.observed_at))
            if kind == 'reputation':
                fact = pack(replace(value, state='available', available_at=value.observed_at))
            identity = dict(tenant_id=tenant, key_hash=key, observed_at=value.observed_at, source_version=value.source_version)
            values = dict(identity, id=uuid4(), kind=kind, available_at=value.available_at, valid_until=value.valid_until,
                          fact_hash=digest((fact, record.provenance if kind == 'security' else value.provenance)), record=body)
            self.session.execute(insert(tables.snapshot).values(**values).on_conflict_do_nothing())
            previous = self.session.execute(select(tables.snapshot).where(match_columns(tables.snapshot, **identity))
                                            .with_for_update()).mappings().one()
            if previous['fact_hash'] != values['fact_hash']:
                raise RecordConflict('Conflicting provider source revision')
            stored = unpack(previous['record'])
            stored_value = stored.snapshot if kind == 'security' else stored.evidence
            available = min(stored_value.available_at, value.available_at)
            is_stale = state == 'stale' or (stored.state if kind == 'security' else stored.evidence.state) == 'stale'
            if kind == 'reputation':
                merged = replace(record, evidence=replace(value, available_at=available,
                                                          state='stale' if is_stale else 'available'))
            else:
                merged = replace(record, snapshot=replace(value, available_at=available),
                                 state='stale' if is_stale else 'available')
            self.session.execute(tables.snapshot.update().where(tables.snapshot.c.id == previous['id']).values(
                available_at=available, record=pack(merged)))

    def _latest_snapshot(self, tenant, key, at):
        supported_time(at, 'cache knowledge time')
        row = self.session.execute(select(tables.snapshot.c.record).where(tables.snapshot.c.tenant_id == tenant,
            tables.snapshot.c.key_hash == key, tables.snapshot.c.observed_at <= at, tables.snapshot.c.available_at <= at,
            tables.snapshot.c.received_at <= at).order_by(tables.snapshot.c.observed_at.desc(),
            tables.snapshot.c.source_version.desc()).limit(1)).scalar_one_or_none()
        return unpack(row) if row else None

    def reputation(self, key, at):
        record = self._latest_snapshot(key.tenant, digest(pack(key)), at)
        return select_reputation(key, (record,), at) if record else None

    def security(self, scope, provider, at):
        record = self._latest_snapshot(scope.payer.tenant, digest(pack((scope, provider))), at)
        return select_security(scope, provider, (record,), at) if record else None

    def bank_links(self, payer):
        rows = self.session.execute(select(tables.link.c.provider, tables.link.c.operation_id, tables.pix.c.external_id).join(
            tables.pix, and_(tables.pix.c.tenant_id == tables.link.c.tenant_id, tables.pix.c.id == tables.link.c.pix_id)).where(
            tables.pix.c.tenant_id == payer.tenant, tables.pix.c.payer_branch == payer.branch,
            tables.pix.c.payer_account == payer.account, tables.pix.c.payer_document == payer.document)).all()
        return tuple(BankLink(payer.tenant, row.provider, row.operation_id, row.external_id) for row in rows)

    # Request receipts

    def insert_receipt(self, receipt_id, request_id, tenant, external_id, payload_hash):
        self.session.execute(tables.receipt.insert().values(id=receipt_id, request_id=request_id, tenant_id=tenant,
                                                            external_id=external_id, payload_hash=payload_hash))

    def receipt_for_update(self, receipt_id, tenant):
        return self.session.execute(select(tables.receipt).where(match_columns(tables.receipt, id=receipt_id, tenant_id=tenant))
                                    .with_for_update()).mappings().first()

    def update_open_receipt(self, receipt_id, tenant, **values):
        """Updates a receipt that has no HTTP outcome yet; returns the number of rows changed."""
        return self.session.execute(tables.receipt.update().where(match_columns(tables.receipt, id=receipt_id, tenant_id=tenant),
            tables.receipt.c.http_status.is_(None)).values(**values)).rowcount

    # Bank challenges, operation links and events

    def challenges(self, tenant, pix_row_id, provider, challenge_id):
        return self.session.execute(select(tables.challenge).where(tables.challenge.c.tenant_id == tenant,
            (tables.challenge.c.pix_id == pix_row_id) | and_(tables.challenge.c.provider == provider,
                                                          tables.challenge.c.challenge_id == challenge_id))).mappings().all()

    def insert_challenge(self, tenant, provider, challenge_id, pix_row_id, analysis_key, operation_fingerprint):
        self.session.execute(tables.challenge.insert().values(tenant_id=tenant, provider=provider, challenge_id=challenge_id,
            pix_id=pix_row_id, analysis_key=analysis_key, operation_fingerprint=operation_fingerprint))

    def challenge_binding(self, tenant, provider, challenge_id, pix_row_id):
        return self.session.execute(select(tables.challenge).where(match_columns(tables.challenge, tenant_id=tenant,
            provider=provider, challenge_id=challenge_id, pix_id=pix_row_id))).mappings().first()

    def bank_operation_links(self, tenant, pix_row_id, provider, operation_id):
        return self.session.execute(select(tables.link).where(tables.link.c.tenant_id == tenant, tables.link.c.provider == provider,
            (tables.link.c.pix_id == pix_row_id) | (tables.link.c.operation_id == operation_id))).mappings().all()

    def insert_bank_operation_link(self, tenant, provider, operation_id, pix_row_id, operation_fingerprint):
        self.session.execute(tables.link.insert().values(tenant_id=tenant, provider=provider, operation_id=operation_id,
            pix_id=pix_row_id, operation_fingerprint=operation_fingerprint))

    def event_payload_hash(self, tenant, provider, event_id):
        return self.session.execute(select(tables.event.c.payload_hash).where(match_columns(tables.event, tenant_id=tenant,
            provider=provider, event_id=event_id))).scalar_one_or_none()

    def events_of_kind(self, tenant, pix_row_id, kind):
        return self.session.execute(select(tables.event).where(match_columns(tables.event, tenant_id=tenant,
            pix_id=pix_row_id, kind=kind))).mappings().all()

    def insert_event(self, **values):
        self.session.execute(tables.event.insert().values(id=uuid4(), **values))
