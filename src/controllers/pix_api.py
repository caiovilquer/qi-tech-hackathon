"""HTTP application service for Pix: durable claims, provider I/O outside locks and final revalidation."""
import asyncio
from dataclasses import replace
from time import monotonic

from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout

from antifraud.adapter import to_pix
from antifraud.contract import parse_payload_json, normalize_payload, ValidationError
from antifraud.engine import preliminary
from controllers.pix_storage import is_lock_timeout, validate_identity
from dtos.pix_storage import Busy, HashConflict, Replay, EventInput, OwnershipConflict
from errors.pix import PixError
from providers.budget import Deadline, BudgetExceeded
from providers.contracts import ReputationKey, SecurityScope, ProviderDataError, ReconciliationRequired
from providers.enrichment import Enricher, EnrichmentNeedsRefresh, SourceCheck
from providers.selection import select_security, select_reputation, reconcile_payments
from utils.logger import get_logger, log_fields
from utils.pix_deadline import in_database, DatabaseClock
from utils.pix_storage_codec import fingerprint

logger = get_logger(__name__)

MAX_REVALIDATION_CYCLES = 3
ERROR_RECEIPT_MARGIN_SECONDS = .1
CLAIM_RETRY_SECONDS = .025
RECEIPT_ATTEMPTS = 2
UNIQUE_VIOLATION = '23505'
PIX_IDENTITY_CONSTRAINT = 'uq_pix_tenant_external_id'
REPUTATION_OPERATION = 'person_reputation'
REPUTATION_SCOPE = 'pix'


class ContextRefreshRequired(RuntimeError):
    """Release the locks and obtain/validate external material in another cycle."""


class PixController:
    def __init__(self, storage, bank, reputation):
        self.storage, self.bank, self.reputation = storage, bank, reputation

    async def receive(self, request_id, tenant, deadline):
        return await in_database(deadline, self.storage.receive, request_id, tenant)

    async def finish(self, receipt, tenant, status, category, deadline, *, key=None, strict=False):
        if receipt is None:
            return
        for attempt in range(1, RECEIPT_ATTEMPTS + 1):
            try:
                await in_database(deadline, self.storage.finish_receipt, receipt, tenant, status, category, analysis_key=key)
                return
            except Exception as error:
                # Pool checkout never reached PostgreSQL, so repeating the write cannot duplicate it.
                if isinstance(error, PoolTimeout) and attempt < RECEIPT_ATTEMPTS:
                    logger.warning('pix.receipt.outcome_retry ' + log_fields(tenant=tenant, status=status, attempt=attempt))
                    continue
                logger.exception('pix.receipt.outcome_failed ' + log_fields(tenant=tenant, status=status, category=category))
                if strict:
                    raise
                return

    async def get(self, tenant, external_id, deadline):
        try:
            validate_identity(tenant, external_id)
        except ValueError:
            # An ID outside the accepted format can never have been stored: the same not-found outcome.
            logger.info('pix.query.not_found ' + log_fields(tenant=tenant, pix_id=external_id, reason='invalid_id'))
            raise PixError(404, 'QIT002001')
        try:
            analysis = await in_database(deadline, self.storage.get, tenant, external_id)
        except Exception as error:
            logger.exception('pix.query.failed ' + log_fields(tenant=tenant, pix_id=external_id))
            raise PixError() from error
        if analysis is None:
            logger.info('pix.query.not_found ' + log_fields(tenant=tenant, pix_id=external_id))
            raise PixError(404, 'QIT002001')
        return analysis.public

    async def wait_claim(self, operation, deadline, *args):
        while True:
            try:
                return await in_database(deadline, operation, *args,
                                         lock_timeout_ms=max(1, min(1000, int(deadline.remaining() * 1000))))
            except Busy as busy:
                logger.debug('pix.claim.busy ' + log_fields(reason=str(busy)))
                await asyncio.sleep(min(CLAIM_RETRY_SECONDS, deadline.remaining()))

    async def finalize(self, deadline, ticket, normalized, enriched, policy, receipt, tenant):
        """Waits for the account/destination advisory locks within the request budget, as wait_claim does.

        Concurrent payers to one destination legitimately serialize on its lock; a lock timeout
        rolled the attempt back, so it is retried instead of being reported as a technical failure.
        """
        while True:
            try:
                return await in_database(deadline, self._finalize_analysis, ticket, normalized, enriched, policy,
                                         receipt, tenant,
                                         lock_timeout_ms=max(1, min(1000, int(deadline.remaining() * 1000))))
            except DBAPIError as error:
                if not is_lock_timeout(error):
                    raise
                logger.debug('pix.analysis.lock_busy ' + log_fields(pix_id=ticket.external_id))
                await asyncio.sleep(min(CLAIM_RETRY_SECONDS, deadline.remaining()))

    def _reputation_key(self, tenant, document):
        return ReputationKey(tenant, document, self.reputation.identity, REPUTATION_OPERATION, REPUTATION_SCOPE)

    def _read_context(self, pix, policy):
        """Local facts, cached provider snapshots and bank links, read outside any lock."""
        with self.storage.read() as repository:
            clock = DatabaseClock(repository.now())
            now = clock()
            keys = tuple(self._reputation_key(pix.payer.tenant, document)
                         for document in (pix.payer.document, pix.payee_document))
            cached = tuple(record for key in keys if (record := repository.reputation(key, now)) is not None)
            return (repository.context(pix, now, policy), cached,
                    repository.security(SecurityScope.from_pix(pix), self.bank.identity, now),
                    repository.bank_links(pix.payer), clock)

    def _persist_enrichment(self, enrichment):
        """Persists provider snapshots, then ingests reconciled bank executions as durable local facts."""
        with self.storage.write() as repository:
            repository.put_snapshots(enrichment.cache_updates + (enrichment.security,))
        with self.storage.read() as repository:
            base = repository.context(enrichment.context.pix, repository.now(), enrichment.progress.policy)
            local_analyses = {analysis.pix.id: analysis for analysis in base.history
                              if analysis.pix.payer == base.pix.payer}

            def event_identity(event):
                return event.payer, event.pix_id, event.event_id

            known_events = {event_identity(event): event for event in base.execution_events}
            new_events = []
            for candidate in enrichment.context.execution_events:
                previous = known_events.get(event_identity(candidate))
                if previous is None:
                    new_events.append(candidate)
                elif (previous.state, previous.occurred_at) != (candidate.state, candidate.occurred_at):
                    raise ReconciliationRequired('Conflicting bank execution identity')
            events = []
            for event in new_events:
                if event.available_at > base.decision_at or event.occurred_at > base.decision_at:
                    continue  # Not known yet: it must not gain a local receipt now.
                original = local_analyses.get(event.pix_id)
                if original is None:
                    raise ProviderDataError('Linked execution has no local analysis')
                row = repository.analysis_row(base.pix.payer.tenant, event.pix_id)
                provider, event_id = event.event_id.split(':', 1)
                events.append(EventInput(base.pix.payer.tenant, provider, event_id, 'execution', event.payer, event.pix_id,
                                         str(row['key']), fingerprint(original.pix), event.state, event.occurred_at))
        for event in events:
            self.storage.ingest(event)

    def _finalize_analysis(self, ticket, normalized, enriched, policy, receipt, tenant, *, lock_timeout_ms=1000):
        """Under the account locks: re-read local facts, revalidate external material, decide and persist."""
        pix = to_pix(normalized, tenant.name)
        with self.storage.analysis(ticket, pix, lock_timeout_ms=lock_timeout_ms) as work:
            now = work.now()
            repository = work.repository
            local = work.context(pix, now, policy)
            known_rows = tuple(row for row in enriched.progress.bank_rows
                               if row.execution.available_at <= now and row.execution.occurred_at <= now)
            reconciled = reconcile_payments(local, known_rows, repository.bank_links(pix.payer))
            if set(reconciled.execution_events) - set(local.execution_events):
                raise ContextRefreshRequired('A new bank link requires ingestion outside final locks')
            local = reconciled
            scope = SecurityScope.from_pix(pix)
            security = select_security(scope, self.bank.identity, (enriched.security,), now,
                                       cached=repository.security(scope, self.bank.identity, now))
            if security != enriched.security:
                raise ContextRefreshRequired('Bank context changed during enrichment')
            if security.state == 'stale':
                raise ContextRefreshRequired('Bank context expired under locks')
            evidence = []
            actions = dict(enriched.context.dict_actions)
            for role, document in (('payer', pix.payer.document), ('payee', pix.payee_document)):
                key = self._reputation_key(tenant.name, document)
                rows = tuple(record for record in enriched.progress.selected if record.key == key)
                stored = repository.reputation(key, now)
                selected = select_reputation(key, rows, now, cached=stored) if rows or stored else None
                previous = select_reputation(key, rows, now) if rows else None
                if stored is not None and selected != previous:
                    raise ContextRefreshRequired('Reputation changed during enrichment')
                if actions.get(role) == 'cache' and (selected is None or not selected.evidence.valid(now)):
                    raise ContextRefreshRequired('Cached reputation expired under locks')
                if selected is not None and (role in enriched.context.dict_queried or selected.evidence.valid(now)):
                    evidence.append((role, selected.evidence))
                    if role not in enriched.context.dict_queried:
                        actions[role] = 'cache'
            context = replace(local, security=security.snapshot, bank_payments=local.bank_payments,
                              dict_evidence=tuple(evidence), dict_queried=enriched.context.dict_queried,
                              dict_actions=tuple(actions.items()), provider_traces=enriched.context.provider_traces)
            if actions.get('payee') == 'skipped_confidence' and preliminary(context, policy).c_pre < policy.dict_threshold:
                raise ContextRefreshRequired('Locked context now requires recipient lookup')
            return work.complete(normalized, context, policy, receipt_id=receipt, source_participant=tenant.participant)

    @staticmethod
    def _readable_id(candidate):
        return (isinstance(candidate, str) and candidate.isprintable() and 1 <= len(candidate) <= 128
                and candidate not in ('.', '..') and not any(char in candidate for char in '/?#'))

    @staticmethod
    def _lost_identity_race(error):
        """True only for the named tenant/ID uniqueness violation: another request committed first."""
        diagnostics = getattr(error.orig, 'diag', None)
        return (getattr(error.orig, 'pgcode', None) == UNIQUE_VIOLATION
                and getattr(diagnostics, 'constraint_name', None) == PIX_IDENTITY_CONSTRAINT)

    @staticmethod
    def _log_outcome(tenant, pix_id, http_status, public, started_at, cycles):
        metadata = public['metadata']
        logger.info('pix.analysis.completed ' + log_fields(
            tenant=tenant.name, pix_id=pix_id, http_status=http_status,
            outcome='replay' if http_status == 200 else 'analysis', status=public['status'], key=public['key'],
            score=metadata['score'], confidence_pre=metadata['confidence_pre'], reasons=metadata['reasons'],
            cycles=cycles, elapsed_ms=(monotonic() - started_at) * 1000))

    async def post(self, raw_body, tenant, policy, receipt, request_deadline):
        started_at = monotonic()
        # Keep a slice of the same budget to persist the outcome of an error receipt.
        deadline = Deadline(request_deadline.expires_at - ERROR_RECEIPT_MARGIN_SECONDS, request_deadline.clock)
        ticket = initial = None
        pix_id = None
        cycles = 0
        field = None
        try:
            parsed = parse_payload_json(raw_body)
            candidate_id = parsed.get('id')
            pix_id = candidate_id if self._readable_id(candidate_id) else None
            try:
                normalized = normalize_payload(parsed)
            except ValidationError:
                # An invalid Pix is never a claim/analysis; keep a readable ID on the receipt without inventing a hash.
                if receipt is not None and pix_id is not None:
                    try:
                        await in_database(deadline, self.storage.identify_receipt, receipt, tenant.name, pix_id, None)
                    except Exception:
                        logger.exception('pix.receipt.identity_failed ' + log_fields(tenant=tenant.name, pix_id=pix_id))
                raise
            if receipt is None:
                raise PixError()
            pix = to_pix(normalized, tenant.name)
            pix_id = pix.id
            await in_database(deadline, self.storage.identify_receipt, receipt, tenant.name, pix.id, normalized.payload_hash)
            ticket = await self.wait_claim(self.storage.claim, deadline, tenant.name, pix.id, normalized.payload_hash)
            if isinstance(ticket, Replay):
                await self.finish(receipt, tenant.name, 200, 'replay', request_deadline,
                                  key=ticket.dto.public['key'], strict=True)
                self._log_outcome(tenant, pix_id, 200, ticket.dto.public, started_at, cycles)
                return 200, ticket.dto.public
            initial = await self.wait_claim(self.storage.claim_initial, deadline, ticket, pix.payer)
            progress = None

            async def complete_initial(result):
                nonlocal initial
                if initial is not None:
                    await in_database(deadline, self.storage.complete_initial, initial, result)
                    initial = None

            for cycles in range(1, MAX_REVALIDATION_CYCLES + 1):
                base, cached, security, links, clock = await in_database(deadline, self._read_context, pix, policy)
                try:
                    enriched = await Enricher(self.bank, self.reputation, policy, clock=clock).enrich(
                        base, source_check=SourceCheck.OWNED if initial else SourceCheck.COMPLETED,
                        deadline=deadline, cached=cached, security_cached=security, links=links, resume=progress,
                        on_initial=complete_initial)
                except EnrichmentNeedsRefresh as refresh:
                    logger.debug('pix.enrichment.refresh ' + log_fields(pix_id=pix_id, cycle=cycles, reason=str(refresh)))
                    progress = refresh.partial
                    if progress.initial_result is not None:
                        await complete_initial(progress.initial_result)
                    continue
                progress = enriched.progress
                await in_database(deadline, self._persist_enrichment, enriched)
                try:
                    dto = await self.finalize(deadline, ticket, normalized, enriched, policy, receipt, tenant)
                except ContextRefreshRequired as refresh:
                    logger.debug('pix.context.refresh ' + log_fields(pix_id=pix_id, cycle=cycles, reason=str(refresh)))
                    continue
                self._log_outcome(tenant, pix_id, 201, dto.public, started_at, cycles)
                return 201, dto.public
            raise BudgetExceeded('Context did not stabilize in three cycles')
        except ValidationError as error:
            field = error.path or None
            outcome = PixError(error.http_status, error.code, field=field)
        except HashConflict:
            outcome = PixError(409, 'QIT002002')
        except (OwnershipConflict, ReconciliationRequired) as conflict:
            logger.warning('pix.analysis.reconciliation_required '
                           + log_fields(tenant=tenant.name, pix_id=pix_id, reason=str(conflict)))
            if ticket is not None and not isinstance(ticket, Replay):
                try:
                    await in_database(request_deadline, self.storage.release_reservations, ticket, initial)
                except Exception:
                    logger.exception('pix.reservation.release_failed ' + log_fields(tenant=tenant.name, pix_id=pix_id))
            outcome = PixError(409, 'QIT002010')
        except DBAPIError as error:
            if self._lost_identity_race(error):
                try:
                    winner = await in_database(deadline, self.storage.resolve_duplicate, tenant.name, pix.id,
                                               normalized.payload_hash)
                    if isinstance(winner, Replay):
                        await self.finish(receipt, tenant.name, 200, 'replay', request_deadline,
                                          key=winner.dto.public['key'], strict=True)
                        self._log_outcome(tenant, pix_id, 200, winner.dto.public, started_at, cycles)
                        return 200, winner.dto.public
                except HashConflict:
                    logger.info('pix.analysis.error ' + log_fields(tenant=tenant.name, pix_id=pix_id, http_status=409,
                                                                  code='QIT002002', elapsed_ms=(monotonic() - started_at) * 1000))
                    await self.finish(receipt, tenant.name, 409, 'QIT002002', request_deadline)
                    raise PixError(409, 'QIT002002')
                except Exception:
                    logger.exception('pix.race.winner_unreadable ' + log_fields(tenant=tenant.name, pix_id=pix_id))
            else:
                logger.exception('pix.analysis.database_error ' + log_fields(tenant=tenant.name, pix_id=pix_id, cycle=cycles))
            outcome = PixError()
        except BudgetExceeded as error:
            logger.warning('pix.analysis.budget_exceeded '
                           + log_fields(tenant=tenant.name, pix_id=pix_id, cycle=cycles, reason=str(error)))
            outcome = PixError()
        except Exception:
            logger.exception('pix.analysis.failed ' + log_fields(tenant=tenant.name, pix_id=pix_id, cycle=cycles))
            outcome = PixError()
        logger.info('pix.analysis.error ' + log_fields(tenant=tenant.name, pix_id=pix_id, http_status=outcome.http_status,
                                                      code=outcome.code, field=field,
                                                      elapsed_ms=(monotonic() - started_at) * 1000))
        await self.finish(receipt, tenant.name, outcome.http_status, outcome.code, request_deadline)
        raise outcome
