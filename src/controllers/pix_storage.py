"""Transaction coordinator for Pix persistence: claims, leases, locks and commits.

No routes, no provider I/O and no SQL here; statements live in PixRepository.
"""
from collections import Counter
from contextlib import contextmanager
from dataclasses import asdict
from datetime import timedelta, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4
import re

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from repositories.pix_repository import PixRepository
from dtos.pix_storage import (ClaimTicket, InitialTicket, Replay, Busy, LostClaim, HashConflict,
                              EventInput, EventConflict, InvalidEvent)
from antifraud.adapter import to_pix
from antifraud.contract import normalize_json
from antifraud.dto import PixTransactionDTO, _freeze
from antifraud.engine import evaluate
from providers.contracts import ReputationRecord, account, ProviderIdentity
from utils.pix_storage_codec import pack, digest, fingerprint
from utils.pix_deadline import bind_session_deadline

LEASE_DURATION = timedelta(seconds=10)
LOCK_NOT_AVAILABLE = '55P03'  # PostgreSQL lock_timeout
STATEMENT_TIMEOUT_MS = 3000
MAX_RAW_BODY_BYTES = 65536
TENANT_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
SHA256_HEX = re.compile(r'[a-f0-9]{64}')
ISPB = re.compile(r'[0-9]{8}')


def validate_identity(tenant, external_id=None, payload_hash=None):
    if not isinstance(tenant, str) or TENANT_NAME.fullmatch(tenant) is None:
        raise ValueError('Invalid tenant')
    if external_id is not None and (not isinstance(external_id, str) or not external_id.isprintable()
                                    or not 1 <= len(external_id) <= 128):
        raise ValueError('Invalid external ID')
    if payload_hash is not None and (not isinstance(payload_hash, str) or SHA256_HEX.fullmatch(payload_hash) is None):
        raise ValueError('Invalid canonical hash')


def is_lock_timeout(error):
    return isinstance(error, DBAPIError) and getattr(error.orig, 'pgcode', None) == LOCK_NOT_AVAILABLE


def validate_binding(tenant, provider, external_id, operation_fingerprint, analysis_key=None, *, require_key=False):
    """Validates a bank binding at the boundary; every rejection is InvalidEvent (QIT002007)."""
    try:
        validate_identity(tenant, external_id)
        ProviderIdentity(provider, 'v1')
        if not isinstance(operation_fingerprint, str) or SHA256_HEX.fullmatch(operation_fingerprint) is None:
            raise ValueError('Invalid financial fingerprint')
        if analysis_key is None and require_key:
            raise ValueError('Analysis key is required for a bank binding')
        if analysis_key is not None and not isinstance(analysis_key, str):
            raise ValueError('Analysis key must be a UUID string')
        return str(UUID(analysis_key)) if analysis_key is not None else None
    except (TypeError, ValueError, AttributeError) as error:
        raise InvalidEvent(str(error)) from error


def require_bank_reference(value, label):
    if not isinstance(value, str) or not value.strip() or not value.isprintable() or len(value) > 128:
        raise InvalidEvent(f'Explicit {label} required')
    return value


class AnalysisWork:
    """One analysis held under the account locks with a valid operation claim."""

    def __init__(self, repository, ticket, pix):
        self.repository, self.ticket, self.pix = repository, ticket, pix
        self.locked_at = repository.now()

    def now(self):
        """PostgreSQL UTC clock under the held locks; decision_at must be captured with it."""
        return self.repository.now()

    def context(self, pix, at, policy):
        if pix != self.pix:
            raise ValueError('Locked operation does not match context')
        return self.repository.context(pix, at, policy)

    @staticmethod
    def same_local_facts(context, current):
        """The decision must use exactly the persisted local facts known at decision_at."""
        if (Counter(context.history) != Counter(current.history)
                or Counter(context.auth_events) != Counter(current.auth_events)):
            raise ValueError('Local history/auth events changed; rebuild context under the held locks')
        if Counter(context.execution_events) == Counter(current.execution_events):
            return
        if set(context.execution_events) - set(current.execution_events):
            raise ValueError('Execution events are not persisted locally; ingest linked bank executions before the final transaction')
        raise ValueError('Local execution events changed; rebuild context under the held locks')

    def complete(self, normalized, context, policy, *, receipt_id=None, raw_body=None, source_participant=None):
        repository, ticket = self.repository, self.ticket
        repository.require_claim(ticket)
        if (context.pix != self.pix or to_pix(normalized, ticket.tenant) != self.pix
                or normalized.payload_hash != ticket.payload_hash or self.pix.id != ticket.external_id):
            raise ValueError('Payload/context does not match permanent reservation')
        if repository.initial(self.pix.payer) is None:
            raise LostClaim('Source initial check is not durably completed')
        now = repository.now()
        if context.decision_at < self.locked_at:
            raise ValueError('decision_at precedes the account locks; capture it with work.now() after locking')
        if context.decision_at > now:
            raise ValueError('decision_at is ahead of the PostgreSQL clock; capture it with work.now()')
        self.same_local_facts(context, repository.context(self.pix, context.decision_at, policy))
        if context.security is not None and not context.security.valid(context.decision_at):
            raise ValueError('Known expired bank context must be refreshed before persistence')
        if raw_body is not None:
            if (not isinstance(raw_body, bytes) or len(raw_body) > MAX_RAW_BODY_BYTES
                    or normalize_json(raw_body).payload_hash != ticket.payload_hash):
                raise ValueError('Raw official body must match canonical hash and size limit')
        decision = evaluate(context, policy)
        receipt_row = None
        if receipt_id is not None:
            receipt_row = repository.receipt_for_update(receipt_id, ticket.tenant)
            if (receipt_row is None or receipt_row['http_status'] is not None
                    or receipt_row['external_id'] not in (None, ticket.external_id)
                    or receipt_row['payload_hash'] not in (None, ticket.payload_hash)):
                raise ValueError('Receipt identity does not match the original request')
        analysis_key, pix_row_id = uuid4(), uuid4()
        dto = PixTransactionDTO.from_decision(str(analysis_key), normalized, context, policy, decision)
        if source_participant is not None:
            if not isinstance(source_participant, str) or ISPB.fullmatch(source_participant) is None:
                raise ValueError('Invalid server source participant')
            audit = dto.audit
            audit['source_participant'] = source_participant
            dto = PixTransactionDTO(dto.public_json, _freeze(audit))
        pix, payload = self.pix, normalized.canonical_payload
        pix_values = dict(id=pix_row_id, tenant_id=ticket.tenant, external_id=pix.id, payload_hash=ticket.payload_hash,
            payload=payload, raw_body=raw_body, received_at=receipt_row['received_at'] if receipt_row else now,
            amount_cents=pix.amount_cents, payer_branch=pix.payer.branch, payer_account=pix.payer.account,
            payer_document=pix.payer.document, payee_document=pix.payee_document, participant=pix.destination.participant,
            destination_branch=pix.destination.branch, destination_account=pix.destination.account,
            transaction_at=pix.transaction_at, source_opened_at=payload['source_account']['opening_date'],
            destination_opened_at=pix.destination_opened_at, device_id=pix.device_id, channel=payload['source']['channel'],
            ip=payload['source']['ip'], geolocation=payload['source'].get('geolocation'))
        analysis_values = dict(key=analysis_key, tenant_id=ticket.tenant, pix_id=pix_row_id, status=decision.status,
            decision_at=context.decision_at, policy_version=policy.version, score=decision.score,
            device_state=decision.device_state, night_qualifies=decision.night_qualifies,
            public_response=dto.public, snapshot=dto.audit)
        repository.record_analysis(pix_values, analysis_values)
        repository.complete_claim(ticket.tenant, ticket.external_id, analysis_key)
        if receipt_id is not None:
            if repository.update_open_receipt(receipt_id, ticket.tenant, http_status=201, category='analysis',
                                              analysis_key=analysis_key, external_id=ticket.external_id,
                                              payload_hash=ticket.payload_hash) != 1:
                raise ValueError('Receipt missing, finalized or outside tenant')
        repository.session.flush()
        return dto  # Internal value; the coordinator context must commit before HTTP success.


class PixStorage:
    """Each public method is one short, explicit PostgreSQL transaction."""

    def __init__(self, sessions):
        self.sessions = sessions

    @staticmethod
    def _configure(session, lock_timeout_ms):
        # Always override a server default of SERIALIZABLE/REPEATABLE READ.
        connection = session.connection(execution_options={'isolation_level': 'READ COMMITTED'})
        # One round trip for the fixed settings; a request budget then narrows both timeouts.
        connection.execute(text("SELECT set_config('search_path', 'public', true), "
                                "set_config('lock_timeout', :lock, true), set_config('statement_timeout', :statement, true)"),
                           {'lock': f'{lock_timeout_ms}ms', 'statement': f'{STATEMENT_TIMEOUT_MS}ms'})
        bind_session_deadline(session, lock_timeout_ms)

    @staticmethod
    def _validated_lock_timeout(lock_timeout_ms):
        if type(lock_timeout_ms) is not int or not 1 <= lock_timeout_ms <= 1000:
            raise ValueError('Lock timeout must be 1..1000 milliseconds')
        return lock_timeout_ms

    @contextmanager
    def write(self, *, lock_timeout_ms=1000):
        timeout = self._validated_lock_timeout(lock_timeout_ms)
        with self.sessions() as session:
            try:
                self._configure(session, timeout)
                yield PixRepository(SimpleNamespace(db_session=session))
                session.flush()
                session.commit()  # Failures, including commit failures, propagate after rollback.
            except BaseException:
                session.rollback()
                raise

    @contextmanager
    def read(self):
        with self.sessions() as session:
            self._configure(session, 1000)
            yield PixRepository(SimpleNamespace(db_session=session))
            # close() rolls back a read transaction; no commit and no detached ORM state.

    @contextmanager
    def coordination(self, label, *, lock_timeout_ms=1000):
        """Short reservation transaction: waiting on a reservation row means a live owner, not a technical failure."""
        try:
            with self.write(lock_timeout_ms=lock_timeout_ms) as repository:
                yield repository
        except DBAPIError as error:
            if is_lock_timeout(error):
                raise Busy(f'{label} is held by a live owner; retry within the request deadline') from error
            raise

    @staticmethod
    def _completed_analysis(repository, tenant, external_id, payload_hash):
        row = repository.analysis_row(tenant, external_id)
        if row is None:
            return None
        if row['payload_hash'] != payload_hash:
            raise HashConflict('Persisted winner has another canonical payload')
        return Replay(repository.dto(row))

    def resolve_duplicate(self, tenant, external_id, payload_hash):
        """Reads the completed winner after a lost uniqueness race; never acquires another claim."""
        validate_identity(tenant, external_id, payload_hash)
        with self.read() as repository:
            return self._completed_analysis(repository, tenant, external_id, payload_hash)

    def claim(self, tenant, external_id, payload_hash, *, lock_timeout_ms=1000):
        validate_identity(tenant, external_id, payload_hash)
        with self.read() as repository:
            # No row lock: a conflicting hash or a finished analysis answers at once,
            # even while the owner is finalizing and holds the reservation row.
            reservation = repository.claim_state(tenant, external_id)
            if reservation is not None:
                if reservation['payload_hash'] != payload_hash:
                    raise HashConflict('Tenant/ID permanently reserved with another payload')
                winner = self._completed_analysis(repository, tenant, external_id, payload_hash)
                if winner is not None:
                    return winner
                if reservation['state'] == 'completed':
                    raise ValueError('Completed reservation has no analysis')
        owner = uuid4()
        with self.coordination('Operation reservation', lock_timeout_ms=lock_timeout_ms) as repository:
            row = repository.reserve_claim(tenant, external_id, payload_hash, owner, LEASE_DURATION)
            if row['payload_hash'] != payload_hash:
                raise HashConflict('Tenant/ID permanently reserved with another payload')
            if row['state'] == 'completed':
                return Replay(repository.get(tenant, external_id))
            fence, lease_until = row['fencing_token'], row['lease_until']
            if row['owner_id'] != owner:
                if lease_until > repository.now():
                    raise Busy('Operation already has an active owner')
                fence += 1
                lease_until = repository.take_over_claim(tenant, external_id, owner, fence, LEASE_DURATION)
            return ClaimTicket(tenant, external_id, payload_hash, owner, fence, lease_until)

    def release_reservations(self, ticket, initial=None):
        """Relinquishes only this owner's leases; the permanent identity/hash stay intact."""
        with self.write() as repository:
            if repository.release_claim(ticket) and initial is not None:
                repository.release_initial(initial, ticket.external_id)

    def claim_initial(self, ticket, payer, *, lock_timeout_ms=1000):
        account(payer)
        if payer.tenant != ticket.tenant:
            raise ValueError('Initial check outside operation tenant')
        owner = uuid4()
        with self.coordination('Source initial check', lock_timeout_ms=lock_timeout_ms) as repository:
            repository.require_claim(ticket)
            repository.ensure_account(payer)
            row = repository.reserve_initial(payer, ticket.external_id, owner, LEASE_DURATION)
            if row['state'] == 'completed':
                return None
            fence, lease_until = row['fencing_token'], row['lease_until']
            if row['owner_id'] != owner:
                if lease_until > repository.now():
                    raise Busy('Source initial lookup has another live owner')
                fence += 1
                lease_until = repository.take_over_initial(payer, ticket.external_id, owner, fence, LEASE_DURATION)
            return InitialTicket(ticket, payer, owner, fence, lease_until)

    def complete_initial(self, ticket, result, *, lock_timeout_ms=1000):
        if (not isinstance(result, ReputationRecord) or result.key.tenant != ticket.payer.tenant
                or result.key.document != ticket.payer.document):
            raise ValueError('Initial result must match exact source subject/tenant')
        with self.coordination('Source initial check', lock_timeout_ms=lock_timeout_ms) as repository:
            repository.require_claim(ticket.claim)
            row = repository.initial_for_update(ticket.payer)
            if (row is None or row['state'] != 'in_progress' or row['owner_id'] != ticket.owner
                    or row['fencing_token'] != ticket.fence or row['claim_external_id'] != ticket.claim.external_id
                    or row['lease_until'] <= repository.now()):
                raise LostClaim('Initial check owner/token/lease is no longer valid')
            if result.evidence.available_at > repository.now():
                raise ValueError('Cannot complete initial check with unknown future coverage')
            # A conflicting cached revision aborts the whole completion: the reservation
            # stays with its owner for a corrected retry or expires for takeover.
            repository.put_snapshots((result,))
            repository.complete_initial(ticket.payer, result)

    def initial(self, payer):
        with self.read() as repository:
            return repository.initial(payer)

    @contextmanager
    def analysis(self, ticket, pix, *, lock_timeout_ms=1000):
        if pix.payer.tenant != ticket.tenant or pix.id != ticket.external_id:
            raise ValueError('Operation does not match claim tenant/ID')
        with self.write(lock_timeout_ms=lock_timeout_ms) as repository:
            repository.lock(pix)
            repository.require_claim(ticket)
            repository.ensure_account(pix.payer)
            yield AnalysisWork(repository, ticket, pix)

    def get(self, tenant, external_id):
        validate_identity(tenant, external_id)
        with self.read() as repository:
            return repository.get(tenant, external_id)

    def receive(self, request_id, tenant=None, external_id=None, payload_hash=None):
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError('Invalid request ID')
        validate_identity(tenant if tenant is not None else '_receipt', external_id, payload_hash)
        receipt_id = uuid4()
        with self.write() as repository:
            repository.insert_receipt(receipt_id, request_id, tenant, external_id, payload_hash)
        return receipt_id

    def identify_receipt(self, receipt_id, tenant, external_id, payload_hash):
        validate_identity(tenant, external_id, payload_hash)
        with self.write() as repository:
            if repository.update_open_receipt(receipt_id, tenant, external_id=external_id, payload_hash=payload_hash) != 1:
                raise ValueError('Receipt no longer belongs to this request')

    def finish_receipt(self, receipt_id, tenant, status, category, *, analysis_key=None):
        if (type(status) is not int or not 100 <= status <= 599
                or not isinstance(category, str) or not 1 <= len(category) <= 64):
            raise ValueError('Invalid receipt outcome')
        with self.write() as repository:
            if analysis_key is not None:
                analysis_key = UUID(str(analysis_key))
                if not repository.analysis_exists(tenant, analysis_key):
                    raise ValueError('Receipt analysis outside tenant or unknown')
            if repository.update_open_receipt(receipt_id, tenant, http_status=status, category=category,
                                              analysis_key=analysis_key) != 1:
                raise ValueError('Receipt missing, finalized or outside tenant')

    def _linked_analysis(self, repository, tenant, external_id, analysis_key=None, expected_fingerprint=None):
        row = repository.analysis_row(tenant, external_id)
        if row is None or analysis_key is not None and str(row['key']) != str(analysis_key):
            raise InvalidEvent('Unknown operation/analysis binding')
        pix = repository.domain_pix(row)
        if expected_fingerprint is not None and fingerprint(pix) != expected_fingerprint:
            raise InvalidEvent('Financial fingerprint mismatch')
        return row, pix

    def bind_challenge(self, tenant, provider, external_id, analysis_key, challenge_id, operation_fingerprint):
        analysis_key = validate_binding(tenant, provider, external_id, operation_fingerprint, analysis_key, require_key=True)
        challenge_id = require_bank_reference(challenge_id, 'bank challenge')
        with self.write() as repository:
            row, pix = self._linked_analysis(repository, tenant, external_id, analysis_key, operation_fingerprint)
            repository.lock(pix, (('challenge', tenant, provider, challenge_id),))
            if row['status'] != 'two_factor_required':
                raise InvalidEvent('Challenge only binds an original 2FA analysis')
            existing = repository.challenges(tenant, row['id'], provider, challenge_id)
            if existing:
                if (len(existing) == 1 and existing[0]['pix_id'] == row['id'] and existing[0]['provider'] == provider
                        and existing[0]['challenge_id'] == challenge_id):
                    return 'replay'
                raise InvalidEvent('Challenge already linked to another operation')
            repository.insert_challenge(tenant, provider, challenge_id, row['id'], row['key'], operation_fingerprint)
            return 'created'

    def link_bank_operation(self, tenant, provider, operation_id, external_id, operation_fingerprint):
        validate_binding(tenant, provider, external_id, operation_fingerprint)
        operation_id = require_bank_reference(operation_id, 'bank operation ID')
        with self.write() as repository:
            row, pix = self._linked_analysis(repository, tenant, external_id, expected_fingerprint=operation_fingerprint)
            repository.lock(pix, (('bank-operation', tenant, provider, operation_id),))
            existing = repository.bank_operation_links(tenant, row['id'], provider, operation_id)
            if existing:
                if len(existing) == 1 and existing[0]['pix_id'] == row['id'] and existing[0]['operation_id'] == operation_id:
                    return 'replay'
                raise InvalidEvent('Bank operation/local binding is already in use')
            repository.insert_bank_operation_link(tenant, provider, operation_id, row['id'], operation_fingerprint)
            return 'created'

    def ingest(self, event):
        if not isinstance(event, EventInput) or event.tenant != event.payer.tenant:
            raise InvalidEvent('Bank fact must match tenant/account')
        payload = asdict(event)
        payload['payer'] = pack(event.payer)
        payload['occurred_at'] = event.occurred_at.astimezone(timezone.utc).isoformat()
        payload_hash = digest(payload)
        with self.write() as repository:
            row, pix = self._linked_analysis(repository, event.tenant, event.external_id)
            repository.lock(pix, (('bank-event', event.tenant, event.provider, event.event_id),))
            accepted_hash = repository.event_payload_hash(event.tenant, event.provider, event.event_id)
            if accepted_hash is not None:
                if accepted_hash != payload_hash:
                    raise EventConflict('Accepted event ID has different content')
                return 'replay'
            if (pix.payer != event.payer or str(row['key']) != event.analysis_key
                    or fingerprint(pix) != event.operation_fingerprint
                    or event.occurred_at > repository.now() or event.occurred_at < pix.transaction_at):
                raise InvalidEvent('Invalid bank fact scope, chronology or financial binding')
            previous = repository.events_of_kind(event.tenant, row['id'], event.kind)
            if event.kind == 'execution':
                if (event.state not in ('executed', 'failed', 'cancelled') or not event.terminal
                        or event.challenge_id is not None):
                    raise InvalidEvent('Invalid terminal execution state')
                if any(earlier['state'] != event.state or earlier['occurred_at'] != event.occurred_at for earlier in previous):
                    raise InvalidEvent('Execution already has another terminal result')
            elif event.kind == 'auth':
                if event.state not in ('passed', 'failed', 'expired') or event.state != 'failed' and not event.terminal:
                    raise InvalidEvent('Invalid authentication result')
                if (repository.challenge_binding(event.tenant, event.provider, event.challenge_id, row['id']) is None
                        or event.occurred_at < row['decision_at']):
                    raise InvalidEvent('Authentication requires the exact bound challenge')
                order = (event.occurred_at, event.source_version)
                for earlier in previous:
                    if earlier['provider'] != event.provider or earlier['challenge_id'] != event.challenge_id:
                        continue
                    earlier_order = (earlier['occurred_at'], earlier['source_version'])
                    if earlier['terminal'] and (event.terminal and earlier['state'] != event.state
                                                or not event.terminal and order >= earlier_order):
                        raise InvalidEvent('Cannot reopen or change a terminal challenge')
                    if event.terminal and not earlier['terminal'] and earlier_order >= order:
                        raise InvalidEvent('Source order contradicts terminal challenge')
            else:
                raise InvalidEvent('Unsupported event type')
            for earlier in previous:
                if (earlier['provider'] == event.provider and earlier['occurred_at'] == event.occurred_at
                        and earlier['source_version'] == event.source_version
                        and (earlier['state'], earlier['terminal']) != (event.state, event.terminal)):
                    raise InvalidEvent('Conflicting source revision')
            repository.insert_event(tenant_id=event.tenant, provider=event.provider, event_id=event.event_id,
                payload_hash=payload_hash, pix_id=row['id'], analysis_key=row['key'],
                operation_fingerprint=event.operation_fingerprint, kind=event.kind, state=event.state,
                terminal=event.terminal, challenge_id=event.challenge_id, occurred_at=event.occurred_at,
                source_version=event.source_version, payload=payload)
            return 'created'
