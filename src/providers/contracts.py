"""Immutable provider boundaries. A future remote adapter implements the same protocols."""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
from typing import Protocol

from antifraud.context import (AccountKey, BankPayment, DestinationKey, DictEvidence, Pix,
                               SecuritySnapshot, aware)
from antifraud.contract import _document


class ProviderDataError(ValueError):
    """Invalid scope, subject, version or trusted data; never interpreted as clean."""


class ProviderUnavailable(Exception):
    """Transient technical failure; at most one retry is permitted."""


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ProviderDataError(f'Invalid {label}')


def document(value):
    if not isinstance(value, str) or re.fullmatch(r'(?:[0-9]{11}|[0-9A-Z]{12}[0-9]{2})', value) is None:
        raise ProviderDataError('Document must already be normalized')
    try:
        _document(value, 'document')
    except ValueError as error:
        raise ProviderDataError('Invalid document DV') from error


def account(value):
    if not isinstance(value, AccountKey):
        raise ProviderDataError('Invalid account identity')
    if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value.tenant) is None:
        raise ProviderDataError('Invalid tenant')
    if re.fullmatch(r'[0-9]{1,4}', value.branch) is None or re.fullmatch(r'[0-9]{1,20}', value.account) is None:
        raise ProviderDataError('Invalid normalized account')
    document(value.document)


def temporal(value):
    try:
        for stamp in (value.observed_at, value.available_at, value.valid_until):
            aware(stamp)
        observed, known, expires = (t.astimezone(timezone.utc) for t in (value.observed_at, value.available_at, value.valid_until))
        if known < observed or expires <= observed:
            raise ValueError('Incoherent provider timestamps')
    except (ValueError, TypeError, AttributeError, OverflowError) as error:
        raise ProviderDataError('Invalid provider timestamps') from error
    # Data may arrive after expiry; that is stale, not an invalid chronology.


def utc_snapshot(value):
    changes = {name: getattr(value, name).astimezone(timezone.utc)
               for name in ('observed_at', 'available_at', 'valid_until')}
    if hasattr(value, 'restrictions'):
        changes['restrictions'] = tuple(utc_snapshot(r) for r in value.restrictions)
    if isinstance(value, SecuritySnapshot):
        changes['recoveries'] = tuple((occurred.astimezone(timezone.utc), known.astimezone(timezone.utc))
                                      for occurred, known in value.recoveries)
    return replace(value, **changes)


def detect_ownership(payers):
    owners = {}
    for payer in payers:
        account(payer)
        key = (payer.tenant, payer.branch, payer.account)
        if key in owners and owners[key] != payer.document:
            raise ProviderDataError('Account ownership change requires explicit reconciliation')
        owners[key] = payer.document


@dataclass(frozen=True)
class ProviderIdentity:
    name: str
    version: str

    def __post_init__(self):
        for value in (self.name, self.version):
            if not isinstance(value, str) or re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', value) is None:
                raise ProviderDataError('Invalid provider identity/version')


@dataclass(frozen=True)
class SecurityScope:
    payer: AccountKey
    device_id: str
    destination: DestinationKey
    payee_document: str

    def __post_init__(self):
        account(self.payer)
        document(self.payee_document)
        text(self.device_id, 'device')
        d = self.destination
        if not isinstance(d, DestinationKey) or re.fullmatch(r'[0-9A-Z]{8}', d.participant) is None:
            raise ProviderDataError('Invalid destination')
        if re.fullmatch(r'[0-9]{1,4}', d.branch) is None or re.fullmatch(r'[0-9]{1,20}', d.account) is None:
            raise ProviderDataError('Invalid destination account')

    @classmethod
    def from_pix(cls, pix: Pix):
        return cls(pix.payer, pix.device_id, pix.destination, pix.payee_document)


@dataclass(frozen=True)
class ReputationKey:
    tenant: str
    document: str
    provider: ProviderIdentity
    operation: str
    scope: str

    def __post_init__(self):
        if not isinstance(self.tenant, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', self.tenant) is None:
            raise ProviderDataError('Invalid tenant')
        document(self.document)
        if not isinstance(self.provider, ProviderIdentity):
            raise ProviderDataError('Invalid provider')
        if self.operation != 'person_reputation':
            raise ProviderDataError('Unsupported reputation operation')
        text(self.scope, 'permission scope')


@dataclass(frozen=True)
class BankRecord:
    scope: SecurityScope
    provider: ProviderIdentity
    state: str
    snapshot: SecuritySnapshot | None
    provenance: str

    def __post_init__(self):
        if not isinstance(self.scope, SecurityScope) or not isinstance(self.provider, ProviderIdentity):
            raise ProviderDataError('Invalid bank record scope/provider')
        text(self.provenance, 'provenance')
        if self.state not in ('available', 'not_covered', 'unavailable', 'stale'):
            raise ProviderDataError('Invalid bank coverage')
        if self.state in ('available', 'stale'):
            if not isinstance(self.snapshot, SecuritySnapshot):
                raise ProviderDataError('Covered bank record requires snapshot')
            temporal(self.snapshot)
            for occurred, known in self.snapshot.recoveries:
                if known.astimezone(timezone.utc) < occurred.astimezone(timezone.utc):
                    raise ProviderDataError('Recovery known before occurrence')
            for restriction in self.snapshot.restrictions:
                temporal(restriction)
                document(restriction.document)
                # A bank context delivers restrictions for these exact subjects.
                expected_docs = ({self.scope.payer.document} if restriction.role == 'payer' else
                                 {self.scope.payee_document} if restriction.role == 'payee' else
                                 {self.scope.payer.document, self.scope.payee_document})
                if restriction.document not in expected_docs:
                    raise ProviderDataError('Restriction subject outside security scope')
                expected = self.scope.payer if restriction.role == 'payer' else self.scope.destination
                if restriction.account is not None and restriction.account != expected:
                    raise ProviderDataError('Restriction account outside security scope')
            object.__setattr__(self, 'snapshot', utc_snapshot(self.snapshot))
        elif self.snapshot is not None:
            raise ProviderDataError('Unknown bank coverage must not invent a snapshot')


@dataclass(frozen=True)
class ReputationRecord:
    key: ReputationKey
    evidence: DictEvidence

    def __post_init__(self):
        if not isinstance(self.key, ReputationKey) or not isinstance(self.evidence, DictEvidence):
            raise ProviderDataError('Invalid reputation record')
        if self.evidence.document != self.key.document:
            raise ProviderDataError('Reputation subject mismatch')
        temporal(self.evidence)
        for restriction in self.evidence.restrictions:
            temporal(restriction)
            if restriction.document != self.key.document:
                raise ProviderDataError('Restriction subject mismatch')
            if isinstance(restriction.account, AccountKey) and restriction.account.tenant != self.key.tenant:
                raise ProviderDataError('Restriction outside tenant')
        object.__setattr__(self, 'evidence', utc_snapshot(self.evidence))


@dataclass(frozen=True)
class BankLink:
    tenant: str
    provider: str
    operation_id: str
    local_pix_id: str

    def __post_init__(self):
        for value in (self.tenant, self.provider, self.operation_id, self.local_pix_id):
            text(value, 'bank operation link')


def validate_payment(row, provider):
    if not isinstance(row, BankPayment) or row.provider != provider.name:
        raise ProviderDataError('Bank payment provider mismatch')
    SecurityScope.from_pix(row.pix)
    if row.pix.destination_opened_at.astimezone(timezone.utc) > row.pix.transaction_at.astimezone(timezone.utc):
        raise ProviderDataError('Bank history account opened after transaction')


class BankContextProvider(Protocol):
    """Current remote reads; as_of is a fixture cutoff/request instant, not time travel.

    Return source occurrence/observation and actual knowledge timestamps even
    when later than as_of. Enricher captures receipt and final decision times.
    Translate SDK/network failures into ProviderUnavailable; ConnectionError, TimeoutError and socket.gaierror are
    technical runner failures. File, permission and SSL configuration errors
    propagate; do not translate configuration/programming errors into outages.
    Invalid scope/content raises ProviderDataError without retry. Preserve
    asyncio cancellation; never block the event loop or suppress cancellation.
    """
    identity: ProviderIdentity

    async def security(self, scope: SecurityScope, as_of: datetime, *, timeout_seconds: float) -> BankRecord: ...

    async def payments(self, payer: AccountKey, as_of: datetime, *, timeout_seconds: float) -> tuple[BankPayment, ...]: ...


class ReputationProvider(Protocol):
    """Same timing, technical-error and cancellation contract as BankContextProvider.

    as_of simulates fixture knowledge only; a real DICT lookup returns current
    evidence, including timestamps after request dispatch. SDK-specific transport
    errors must be translated to ProviderUnavailable, data to ProviderDataError.
    """
    identity: ProviderIdentity

    async def reputation(self, key: ReputationKey, as_of: datetime, *, timeout_seconds: float) -> ReputationRecord: ...
