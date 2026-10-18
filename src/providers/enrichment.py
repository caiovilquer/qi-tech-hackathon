"""External enrichment only. Claim ownership, persistence and HTTP remain with the caller."""
from dataclasses import dataclass, field, replace
from enum import Enum
from datetime import datetime, timezone
from typing import Callable

from antifraud.context import Context, ProviderTrace, supported_time
from antifraud.engine import preliminary
from antifraud.policy import Policy
from .budget import Deadline, ProviderRunner
from .contracts import (BankContextProvider, ReputationProvider, BankRecord, ReputationRecord,
                        ProviderDataError, ProviderUnavailable, ReputationKey, SecurityScope, text, validate_payment)
from .selection import select_reputation, select_security, reconcile_payments


def utc_now():
    return datetime.now(timezone.utc)


class EnrichmentNeedsRefresh(Exception):
    """Time changed a prerequisite; caller must revalidate within the same budget."""


class SourceCheck(Enum):
    OWNED = 'owned'
    COMPLETED = 'completed'


@dataclass(frozen=True)
class Enrichment:
    context: Context
    security: BankRecord
    history_state: str
    initial_result: ReputationRecord | None  # Selected coverage, never the raw lookup.
    lookups: tuple[ReputationRecord, ...]  # Raw logical reputation responses.
    cache_updates: tuple[ReputationRecord, ...]
    security_lookup: BankRecord
    received_at: tuple[tuple[str, datetime], ...]


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
                     cached=(), security_cached=None, links=()):
        if not isinstance(source_check, SourceCheck):
            raise ValueError('A proven owned or completed source check is required')
        deadline.remaining()
        scope = SecurityScope.from_pix(base.pix)
        now = base.decision_at.astimezone(timezone.utc)

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
        receipts = []
        if security_cached is not None:
            if not isinstance(security_cached, BankRecord):
                raise ProviderDataError('Invalid security cache record')
            if security_cached.scope != scope or security_cached.provider != self.bank.identity:
                security_cached = None  # Ignore cache of another key, as for reputation.
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
        context = reconcile_payments(base, payments, links)
        context = replace(context, decision_at=now, security=security.snapshot, dict_evidence=(), dict_queried=(), dict_actions=())
        confidence = preliminary(context, self.policy).c_pre
        keys = {role: ReputationKey(base.pix.payer.tenant, doc, self.reputation.identity,
                                   'person_reputation', self.scope)
                for role, doc in (('payer', base.pix.payer.document), ('payee', base.pix.payee_document))}
        cached = tuple(cached)
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
                actions[role] = 'queried'
                queried.append(role)
            else:
                actions[role] = 'skipped_after_initial' if role == 'payer' else 'skipped_confidence'

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
        if (actions['payee'] == 'skipped_confidence'
                and preliminary(context, self.policy).c_pre < self.policy.dict_threshold):
            raise EnrichmentNeedsRefresh('Final context requires an absent recipient lookup')
        evidence = []
        traces = [ProviderTrace('security', security.provider.name, security.provider.version,
                                'security_context', 'account_device_destination', security.provenance,
                                security.state, observed.state),
                  ProviderTrace('history', self.bank.identity.name, self.bank.identity.version,
                                'confirmed_payments', 'payer_account', self.bank.identity.name,
                                history_state, history_state)]
        for role, key in keys.items():
            record = known[key]
            raw = lookups.get(key)
            if actions[role] == 'cache' and not record.evidence.valid(now):
                raise EnrichmentNeedsRefresh('Cached evidence expired during enrichment')
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
        initial = known[keys['payer']] if source_check == SourceCheck.OWNED else None
        deadline.remaining()
        return Enrichment(context, security, history_state, initial, tuple(lookups.values()), updates,
                          observed, tuple(receipts))
