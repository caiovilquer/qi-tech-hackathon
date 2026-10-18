"""External enrichment only. Claim ownership, persistence and HTTP remain with the caller."""
from dataclasses import dataclass, field, replace
from enum import Enum
from datetime import datetime, timezone, timedelta
from typing import Callable

from antifraud.context import Context, Pix, ProviderTrace, supported_time
from antifraud.engine import preliminary
from antifraud.policy import Policy
from .budget import Deadline, ProviderRunner
from .contracts import (BankContextProvider, ReputationProvider, BankRecord, ReputationRecord,
                        ProviderDataError, ProviderIdentity, ProviderUnavailable, ReputationKey, SecurityScope, text, validate_payment)
from .selection import select_reputation, select_security, reconcile_payments


def utc_now():
    return datetime.now(timezone.utc)


class EnrichmentNeedsRefresh(Exception):
    """No decision: preserve request-local progress for revalidation/persistence."""

    def __init__(self, message, partial):
        super().__init__(message)
        self.partial = partial


class SourceCheck(Enum):
    OWNED = 'owned'
    COMPLETED = 'completed'


@dataclass(frozen=True)
class EnrichmentProgress:
    """Immutable continuation bound to one request and its existing deadline.

    This is material for the coordinator, never proof of a durable claim.
    Initial coverage is frozen at completion; selected facts are revalidated.
    """
    pix: Pix
    policy: Policy
    bank_provider: ProviderIdentity
    reputation_provider: ProviderIdentity
    scope: str
    deadline: Deadline
    source_check: SourceCheck
    captured_at: datetime
    security: BankRecord
    security_lookup: BankRecord
    initial_result: ReputationRecord | None
    selected: tuple[ReputationRecord, ...]
    lookups: tuple[ReputationRecord, ...]
    received_at: tuple[tuple[str, datetime], ...]
    bank_rows: tuple = ()

    @property
    def cache_updates(self):
        return tuple(r for r in self.selected if r.evidence.state in ('available', 'stale'))


@dataclass(frozen=True)
class Enrichment:
    context: Context
    security: BankRecord
    history_state: str
    initial_result: ReputationRecord | None  # Selected coverage at initial completion.
    lookups: tuple[ReputationRecord, ...]  # Raw logical reputation responses.
    cache_updates: tuple[ReputationRecord, ...]
    security_lookup: BankRecord
    received_at: tuple[tuple[str, datetime], ...]
    progress: EnrichmentProgress


@dataclass(frozen=True)
class Enricher:
    bank: BankContextProvider
    reputation: ReputationProvider
    policy: Policy
    scope: str = 'pix'
    runner: ProviderRunner = ProviderRunner()
    clock: Callable[[], datetime] = field(default=utc_now, repr=False, compare=False)

    def __post_init__(self):
        text(self.scope, 'permission scope')
        if not isinstance(self.policy, Policy) or not isinstance(self.runner, ProviderRunner) or not callable(self.clock):
            raise ProviderDataError('Invalid enrichment policy/budget/clock')

    async def enrich(self, base: Context, *, source_check: SourceCheck, deadline: Deadline,
                     cached=(), security_cached=None, links=(), resume=None, on_initial=None):
        if not isinstance(source_check, SourceCheck):
            raise ValueError('A proven owned or completed source check is required')
        deadline.remaining()
        scope = SecurityScope.from_pix(base.pix)
        if resume is not None:
            if (not isinstance(resume, EnrichmentProgress) or resume.pix != base.pix
                    or resume.policy != self.policy or resume.scope != self.scope
                    or resume.bank_provider != self.bank.identity or resume.reputation_provider != self.reputation.identity
                    or resume.deadline is not deadline
                    or not (resume.source_check == source_check or
                            source_check == SourceCheck.COMPLETED and resume.initial_result is not None)):
                raise ProviderDataError('Continuation must match request, providers, policy, source check and deadline')
            allowed = {ReputationKey(base.pix.payer.tenant, doc, self.reputation.identity,
                                     'person_reputation', self.scope)
                       for doc in (base.pix.payer.document, base.pix.payee_document)}
            records = resume.selected + resume.lookups + ((resume.initial_result,) if resume.initial_result else ())
            if (any(not isinstance(r, ReputationRecord) or r.key not in allowed for r in records)
                    or resume.initial_result is not None
                    and resume.initial_result.key.document != base.pix.payer.document):
                raise ProviderDataError('Continuation contains out-of-scope reputation material')
        now = base.decision_at.astimezone(timezone.utc)
        if resume is not None:
            now = max(now, resume.captured_at)

        def capture():
            nonlocal now
            value = self.clock()
            try:
                supported_time(value, 'provider receipt/decision time')
                value = value.astimezone(timezone.utc)
                if value < now:
                    raise ValueError('Wall clock precedes context or previous receipt')
            except (ValueError, TypeError, AttributeError, OverflowError) as error:
                raise ProviderDataError('Invalid provider receipt/decision clock') from error
            now = value
            return now

        capture()
        receipts = list(resume.received_at) if resume is not None else []
        initial = resume.initial_result if resume is not None else None
        raw_log = list(resume.lookups) if resume is not None else []
        memo = {record.key: record for record in raw_log}
        if security_cached is not None:
            if not isinstance(security_cached, BankRecord):
                raise ProviderDataError('Invalid security cache record')
            if security_cached.scope != scope or security_cached.provider != self.bank.identity:
                security_cached = None  # Ignore cache of another key, as for reputation.
        if resume is not None:
            retained = (resume.security_lookup, resume.security) + ((security_cached,) if security_cached else ())
            security_cached = select_security(scope, self.bank.identity, retained, now)
        try:
            observed = await self.runner.call(
                lambda seconds: self.bank.security(scope, now, timeout_seconds=seconds), deadline)
        except ProviderUnavailable:
            observed = BankRecord(scope, self.bank.identity, 'unavailable', None, self.bank.identity.name)
        receipts.append(('security', capture()))
        if not isinstance(observed, BankRecord):
            raise ProviderDataError('Invalid bank provider response')
        # Validate scope even if history or subsequent lookups fail.
        select_security(scope, self.bank.identity, (observed,), now, cached=security_cached)
        try:
            payments = await self.runner.call(
                lambda seconds: self.bank.payments(base.pix.payer, now, timeout_seconds=seconds), deadline)
            if type(payments) is not tuple:
                raise ProviderDataError('Bank history must be an immutable tuple')
            for row in payments:
                validate_payment(row, self.bank.identity)
            history_state = 'available' if payments else 'not_covered'
        except ProviderUnavailable:
            payments, history_state = (), 'unavailable'
        receipts.append(('history', capture()))
        security = select_security(scope, self.bank.identity, (observed,), now, cached=security_cached)
        # Ignore executions outside the behavioural window of this request.
        # In particular, a historical bank link need not load an ancient local
        # analysis merely to discard it later in the pure feature calculation.
        payments = tuple(row for row in ((resume.bank_rows if resume else ()) + payments)
                         if base.pix.transaction_at-timedelta(days=self.policy.history_days)
                         < row.pix.transaction_at <= base.pix.transaction_at)
        context = reconcile_payments(base, payments, links)
        context = replace(context, decision_at=now, security=security.snapshot, dict_evidence=(), dict_queried=(), dict_actions=())
        confidence = preliminary(context, self.policy).c_pre
        keys = {role: ReputationKey(base.pix.payer.tenant, doc, self.reputation.identity,
                                   'person_reputation', self.scope)
                for role, doc in (('payer', base.pix.payer.document), ('payee', base.pix.payee_document))}
        cached = tuple(cached) + (resume.selected if resume is not None else ())
        if any(not isinstance(record, ReputationRecord) for record in cached):
            raise ProviderDataError('Invalid cache record')
        known, lookups, actions, queried = {}, {}, {}, []
        for role, key in keys.items():
            if key not in known:
                matching = tuple(r for r in cached if r.key == key)
                known[key] = select_reputation(key, matching, now, ttl_seconds=self.policy.dict_ttl_seconds) if matching else None
            previous = known[key]
            required = source_check == SourceCheck.OWNED if role == 'payer' else confidence < self.policy.dict_threshold
            if previous is not None and previous.evidence.valid(now):
                actions[role] = 'cache'
            elif (key in memo and (role == 'payer' and initial is not None
                    or required and memo[key].evidence.state in ('not_covered', 'unavailable')
                    and memo[key].evidence.available_at <= now)):
                known[key] = select_reputation(key, (memo[key],), now, cached=previous,
                                               ttl_seconds=self.policy.dict_ttl_seconds)
                lookups[key] = memo[key]
                actions[role] = 'queried'
                queried.append(role)
            elif role == 'payer' and initial is not None:
                actions[role] = 'skipped_after_initial'
            elif required:
                if key not in lookups:
                    try:
                        raw = await self.runner.call(
                            lambda seconds, key=key: self.reputation.reputation(key, now, timeout_seconds=seconds), deadline)
                    except ProviderUnavailable:
                        capture()
                        raw = select_reputation(key, (), now, ttl_seconds=self.policy.dict_ttl_seconds)
                        raw = replace(raw, evidence=replace(raw.evidence, state='unavailable'))
                    receipts.append((role, capture()))
                    if not isinstance(raw, ReputationRecord):
                        raise ProviderDataError('Invalid reputation provider response')
                    known[key] = select_reputation(key, (raw,), now, cached=previous,
                                                   ttl_seconds=self.policy.dict_ttl_seconds)
                    lookups[key] = raw
                    memo[key] = raw
                    raw_log.append(raw)
                actions[role] = 'queried'
                queried.append(role)
            else:
                actions[role] = 'skipped_after_initial' if role == 'payer' else 'skipped_confidence'
            if role == 'payer' and source_check == SourceCheck.OWNED and initial is None:
                initial = known[key]
                if on_initial is not None:
                    await on_initial(initial)

        # Final knowledge time follows all responses. Recheck expiry without
        # renewing source validity or replacing an expired watermark with old clean data.
        capture()
        security = select_security(scope, self.bank.identity, (observed,), now, cached=security_cached)
        for key, previous in known.items():
            if previous is not None:
                rows = (lookups[key],) if key in lookups else ()
                known[key] = select_reputation(key, rows, now, cached=previous,
                                               ttl_seconds=self.policy.dict_ttl_seconds)
        context = replace(context, decision_at=now, security=security.snapshot)
        progress = EnrichmentProgress(base.pix, self.policy, self.bank.identity, self.reputation.identity,
            self.scope, deadline, source_check, now, security, observed, initial,
            tuple(r for r in known.values() if r is not None), tuple(raw_log), tuple(receipts), payments)
        if security.state == 'stale':
            raise EnrichmentNeedsRefresh('Known bank security is expired; refresh required', progress)
        if (actions['payee'] == 'skipped_confidence'
                and preliminary(context, self.policy).c_pre < self.policy.dict_threshold):
            raise EnrichmentNeedsRefresh('Final context requires an absent recipient lookup', progress)
        evidence = []
        traces = [ProviderTrace('security', security.provider.name, security.provider.version,
                                'security_context', 'account_device_destination', security.provenance,
                                security.state, observed.state),
                  ProviderTrace('history', self.bank.identity.name, self.bank.identity.version,
                                'confirmed_payments', 'payer_account', self.bank.identity.name,
                                history_state, history_state)]
        for role, key in keys.items():
            record = known[key]
            raw = lookups.get(key) or memo.get(key)
            if actions[role] == 'cache' and not record.evidence.valid(now):
                raise EnrichmentNeedsRefresh('Cached evidence expired during enrichment', progress)
            traces.append(ProviderTrace(role, key.provider.name, key.provider.version, key.operation, key.scope,
                                       record.evidence.provenance if record else key.provider.name,
                                       record.evidence.state if record else None,
                                       raw.evidence.state if raw else None))
            if record is not None and (role in queried or record.evidence.valid(now)):
                evidence.append((role, record.evidence))
                if role not in queried and record.evidence.valid(now):
                    actions[role] = 'cache'
        context = replace(context, dict_evidence=tuple(evidence), dict_queried=tuple(queried),
                          dict_actions=tuple(actions.items()), provider_traces=tuple(traces))
        updates = tuple(record for record in known.values() if record is not None
                        and record.evidence.state in ('available', 'stale'))
        deadline.remaining()
        return Enrichment(context, security, history_state, initial, tuple(raw_log), updates,
                          observed, tuple(receipts), progress)
