"""Explicit deterministic in-memory adapters, not fake network integrations."""
from dataclasses import dataclass
from datetime import timezone

from .budget import positive_seconds
from .contracts import (BankRecord, ReputationRecord, ProviderIdentity,
                        ProviderDataError, ProviderUnavailable, account, SecurityScope, validate_payment,
                        ReputationKey, detect_ownership)
from .selection import select_reputation, select_security
from antifraud.context import BankPayment, aware


@dataclass(frozen=True)
class FixtureBankProvider:
    identity: ProviderIdentity
    records: tuple[BankRecord, ...] = ()
    history: tuple[BankPayment, ...] = ()
    unavailable: tuple[SecurityScope, ...] = ()

    def __post_init__(self):
        if not isinstance(self.identity, ProviderIdentity):
            raise ProviderDataError('Invalid provider identity')
        for name in ('records', 'history', 'unavailable'):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        if any(not isinstance(r, BankRecord) or r.provider != self.identity for r in self.records):
            raise ProviderDataError('Bank dataset provider mismatch')
        if any(not isinstance(s, SecurityScope) for s in self.unavailable):
            raise ProviderDataError('Invalid unavailable scope')
        operations, executions = {}, {}
        for row in self.history:
            validate_payment(row, self.identity)
            key = (row.pix.payer.tenant, row.operation_id)
            event = (row.pix.payer.tenant, row.execution.event_id)
            if key in operations and operations[key] != row:
                raise ProviderDataError('Conflicting bank operation in fixture dataset')
            if event in executions and executions[event] != key:
                raise ProviderDataError('Execution reused by different bank operations')
            operations[key], executions[event] = row, key
        detect_ownership(tuple(r.scope.payer for r in self.records) + tuple(r.pix.payer for r in self.history))

    async def security(self, scope, as_of, *, timeout_seconds):
        positive_seconds(timeout_seconds)
        if not isinstance(scope, SecurityScope):
            raise ProviderDataError('Invalid bank request')
        if scope in self.unavailable:
            raise ProviderUnavailable('Configured synthetic bank outage')
        rows = tuple(r for r in self.records if r.scope == scope)
        return select_security(scope, self.identity, rows, as_of)

    async def payments(self, payer, as_of, *, timeout_seconds):
        positive_seconds(timeout_seconds)
        account(payer)
        aware(as_of)
        as_of = as_of.astimezone(timezone.utc)
        if any(scope.payer == payer for scope in self.unavailable):
            raise ProviderUnavailable('Configured synthetic bank history outage')
        return tuple(r for r in self.history if r.pix.payer == payer and r.execution.available_at <= as_of
                     and r.execution.occurred_at <= as_of and r.pix.transaction_at <= as_of)


@dataclass(frozen=True)
class FixtureReputationProvider:
    identity: ProviderIdentity
    records: tuple[ReputationRecord, ...] = ()
    unavailable: tuple = ()

    def __post_init__(self):
        if not isinstance(self.identity, ProviderIdentity):
            raise ProviderDataError('Invalid provider identity')
        object.__setattr__(self, 'records', tuple(self.records))
        object.__setattr__(self, 'unavailable', tuple(self.unavailable))
        if any(not isinstance(r, ReputationRecord) or r.key.provider != self.identity for r in self.records):
            raise ProviderDataError('Reputation dataset provider mismatch')
        if any(not isinstance(key, ReputationKey) or key.provider != self.identity for key in self.unavailable):
            raise ProviderDataError('Unavailable reputation scope/provider mismatch')

    async def reputation(self, key, as_of, *, timeout_seconds):
        positive_seconds(timeout_seconds)
        if not isinstance(key, ReputationKey) or key.provider != self.identity:
            raise ProviderDataError('Wrong reputation provider/version')
        if key in self.unavailable:
            raise ProviderUnavailable('Configured synthetic reputation outage')
        rows = tuple(r for r in self.records if r.key == key)
        return select_reputation(key, rows, as_of)
