"""Immutable inputs and decisions for the pure Pix engine; no I/O or clock."""
from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
import re

APPROVED, TWO_FA, REJECTED = "approved", "two_factor_required", "rejected"
RANK = {APPROVED: 0, TWO_FA: 1, REJECTED: 2}

def normalized_document(value: str) -> str:
    """Boundary helper: normalize presentation, not registry/DV validation."""
    return re.sub(r"[.\-/\s]", "", value).upper()


def aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone required")


def supported_time(value: datetime, label: str) -> None:
    """Technical arithmetic range, independent of age/current server time."""
    aware(value)
    try:
        valid = 2 <= value.astimezone(timezone.utc).year <= 9998
    except (OverflowError, ValueError):
        valid = False
    if not valid:
        raise ValueError(f"{label} requires UTC year in [2, 9998] for safe time-window arithmetic")


def _immutable(value):
    if isinstance(value, tuple):
        for item in value:
            _immutable(item)
    elif isinstance(value, datetime):
        aware(value)
    elif value is not None and not isinstance(value, (str, int, float, ImmutableValue)):
        raise TypeError("Domain values must be immutable scalars, domain objects or tuples")


class ImmutableValue:
    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            _immutable(value)
            if field.type == "datetime" and not isinstance(value, datetime):
                raise TypeError(f"Invalid datetime: {field.name}")
            if field.type == "str" and not isinstance(value, str):
                raise TypeError(f"Invalid string: {field.name}")
            if field.type == "int" and type(value) is not int:
                raise ValueError(f"Invalid integer: {field.name}")
            if field.type in ("AccountKey", "DestinationKey", "Pix") and not isinstance(value, globals()[field.type]):
                raise TypeError(f"Invalid domain object: {field.name}")
        if hasattr(self, "restrictions") and (type(self.restrictions) is not tuple
                or not all(isinstance(item, Restriction) for item in self.restrictions)):
            raise TypeError("Invalid restrictions")
        if hasattr(self, "recoveries") and (type(self.recoveries) is not tuple or any(
                type(pair) is not tuple or len(pair) != 2 or not all(isinstance(t, datetime) for t in pair)
                for pair in self.recoveries)):
            raise TypeError("Invalid recovery times")
        for name, allowed in getattr(self, "_enums", ()):
            if getattr(self, name) not in allowed:
                raise ValueError(f"Invalid {name}")
        for name in ("fraud_count", "open_reports", "source_version", "sequence"):
            if hasattr(self, name):
                value = getattr(self, name)
                if type(value) is not int or value < (1 if name == "sequence" else 0):
                    raise ValueError(f"Invalid nonnegative integer: {name}")
        for name in ("active", "terminal", "trusted_beneficiary", "bank_limit_exceeded", "night_qualifies"):
            if hasattr(self, name) and type(getattr(self, name)) is not bool:
                raise ValueError(f"Invalid boolean: {name}")


@dataclass(frozen=True)
class AccountKey(ImmutableValue):
    tenant: str
    branch: str
    account: str
    document: str


@dataclass(frozen=True)
class DestinationKey(ImmutableValue):
    participant: str
    branch: str
    account: str


@dataclass(frozen=True)
class Pix(ImmutableValue):
    id: str
    payer: AccountKey
    payee_document: str
    destination: DestinationKey
    amount_cents: int
    transaction_at: datetime
    destination_opened_at: datetime
    device_id: str

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.amount_cents) is not int or self.amount_cents <= 0:
            raise ValueError("amount_cents must be a positive integer")
        if not self.device_id:
            raise ValueError("device_id must be explicit and nonempty")
        supported_time(self.transaction_at, "transaction_at")
        aware(self.destination_opened_at)
        for document in (self.payer.document, self.payee_document):
            if document != normalized_document(document):
                raise ValueError("Documents must already be normalized at the boundary")

    @property
    def payer_pf(self) -> bool:
        return len(self.payer.document) == 11

    @property
    def payee_pf(self) -> bool:
        return len(self.payee_document) == 11

    @property
    def same_holder(self) -> bool:
        return self.payer.document == self.payee_document


@dataclass(frozen=True)
class ExecutionEvent(ImmutableValue):
    _enums = (("state", ("executed", "failed", "cancelled")),)
    event_id: str
    payer: AccountKey
    pix_id: str
    state: str  # executed | failed | cancelled; trusted bank fact
    occurred_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class AuthEvent(ImmutableValue):
    _enums = (("state", ("passed", "failed")),)
    event_id: str
    payer: AccountKey
    pix_id: str
    challenge_id: str
    state: str  # passed | failed
    terminal: bool
    occurred_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class Restriction(ImmutableValue):
    _enums = (("role", ("payer", "payee", "both")),)
    document: str
    role: str  # payer | payee | both
    observed_at: datetime
    available_at: datetime
    valid_until: datetime
    active: bool = True
    account: AccountKey | DestinationKey | None = None

    def __post_init__(self):
        super().__post_init__()
        if self.account is None:
            return
        expected = {"payer": AccountKey, "payee": DestinationKey}.get(self.role)
        # `both` is document-wide. A scoped restriction names one explicit role.
        if expected is None or not isinstance(self.account, expected):
            raise ValueError("Restriction account type must match its role; both requires account=None")

    def applies(self, pix: Pix, role: str, now: datetime) -> bool:
        account = pix.payer if role == "payer" else pix.destination
        document = pix.payer.document if role == "payer" else pix.payee_document
        return (self.active and self.role in (role, "both") and self.document == document
                and self.account in (None, account) and self.observed_at <= now
                and self.available_at <= now < self.valid_until)


@dataclass(frozen=True)
class SecuritySnapshot(ImmutableValue):
    _enums = (("device_state", ("registered", "unregistered", "revoked", "unknown")),)
    observed_at: datetime
    available_at: datetime
    valid_until: datetime
    device_state: str = "unknown"  # registered | unregistered | revoked | unknown
    recoveries: tuple[tuple[datetime, datetime], ...] = ()  # occurred, available
    restrictions: tuple[Restriction, ...] = ()
    trusted_beneficiary: bool = False  # explicit bank designation for THIS account
    bank_limit_exceeded: bool = False  # independent effective bank constraint
    source_version: int = 0  # source revision for equal observed_at, not delivery order

    def valid(self, now: datetime) -> bool:
        return self.observed_at <= now and self.available_at <= now < self.valid_until


@dataclass(frozen=True)
class DictEvidence(ImmutableValue):
    _enums = (("state", ("available", "not_covered", "unavailable", "stale")),)
    document: str
    state: str  # available | not_covered | unavailable | stale
    observed_at: datetime
    available_at: datetime
    valid_until: datetime
    fraud_count: int = 0
    open_reports: int = 0
    restrictions: tuple[Restriction, ...] = ()
    provenance: str = field(kw_only=True)
    source_version: int = 0

    def __post_init__(self):
        super().__post_init__()
        if not self.provenance.strip():
            raise ValueError("Evidence provenance must be explicit and nonblank")

    def valid(self, now: datetime) -> bool:
        return (self.state == "available" and self.observed_at <= now
                and self.available_at <= now < self.valid_until)


@dataclass(frozen=True)
class HistoricalAnalysis(ImmutableValue):
    _enums = (("status", (APPROVED, TWO_FA, REJECTED)), ("device_state", ("registered", "unregistered", "revoked", "unknown")))
    pix: Pix
    decision_at: datetime
    sequence: int
    status: str
    challenge_id: str | None = None
    device_state: str = "unknown"
    night_qualifies: bool = False  # stored designation at original decision


@dataclass(frozen=True)
class BankPayment(ImmutableValue):
    """A confirmed external execution, never a fabricated API analysis/receipt."""
    provider: str
    operation_id: str
    pix: Pix
    execution: ExecutionEvent

    def __post_init__(self):
        super().__post_init__()
        if not self.provider.strip() or not self.operation_id.strip():
            raise ValueError("Explicit bank operation identity required")
        event = self.execution
        if (not isinstance(event, ExecutionEvent) or event.state != "executed"
                or event.payer != self.pix.payer or event.pix_id != self.operation_id
                or self.pix.id != self.operation_id
                or event.occurred_at.astimezone(timezone.utc) < self.pix.transaction_at.astimezone(timezone.utc)
                or event.available_at.astimezone(timezone.utc) < event.occurred_at.astimezone(timezone.utc)):
            raise ValueError("Bank payment requires matching confirmed execution and coherent times")
        object.__setattr__(self, 'pix', replace(self.pix,
            transaction_at=self.pix.transaction_at.astimezone(timezone.utc),
            destination_opened_at=self.pix.destination_opened_at.astimezone(timezone.utc)))
        object.__setattr__(self, 'execution', replace(event, occurred_at=event.occurred_at.astimezone(timezone.utc),
                                                     available_at=event.available_at.astimezone(timezone.utc)))


@dataclass(frozen=True)
class ProviderTrace(ImmutableValue):
    _enums = (("role", ("security", "history", "payer", "payee")),
              ("state", (None, "available", "not_covered", "unavailable", "stale")),
              ("lookup_state", (None, "available", "not_covered", "unavailable", "stale")))
    role: str
    provider: str
    version: str
    operation: str
    permission_scope: str
    provenance: str
    state: str | None
    lookup_state: str | None = None


@dataclass(frozen=True)
class Context(ImmutableValue):
    pix: Pix
    decision_at: datetime
    history: tuple[HistoricalAnalysis, ...] = ()
    execution_events: tuple[ExecutionEvent, ...] = ()
    auth_events: tuple[AuthEvent, ...] = ()
    security: SecuritySnapshot | None = None
    dict_evidence: tuple[tuple[str, DictEvidence], ...] = ()  # (payer/payee, evidence)
    dict_queried: tuple[str, ...] = ()
    dict_actions: tuple[tuple[str, str], ...] = ()
    bank_payments: tuple[BankPayment, ...] = ()
    provider_traces: tuple[ProviderTrace, ...] = ()

    def __post_init__(self):
        super().__post_init__()
        supported_time(self.decision_at, "decision_at")
        for name, kind in (("history", HistoricalAnalysis), ("execution_events", ExecutionEvent),
                           ("auth_events", AuthEvent), ("bank_payments", BankPayment),
                           ("provider_traces", ProviderTrace)):
            if type(getattr(self, name)) is not tuple or not all(isinstance(x, kind) for x in getattr(self, name)):
                raise TypeError(f"Invalid immutable collection: {name}")
        if not isinstance(self.pix, Pix) or self.security is not None and not isinstance(self.security, SecuritySnapshot):
            raise TypeError("Invalid Pix/security context")
        roles = []
        for role, item in self.dict_evidence:
            if role not in ("payer", "payee") or not isinstance(item, DictEvidence) or role in roles:
                raise ValueError("Invalid or duplicate reputation role")
            document = self.pix.payer.document if role == "payer" else self.pix.payee_document
            if item.document != document:
                raise ValueError("DICT subject does not match the role")
            roles.append(role)
        if len(set(self.dict_queried)) != len(self.dict_queried) or any(x not in ("payer", "payee") for x in self.dict_queried):
            raise ValueError("Invalid queried role")
        if len(dict(self.dict_actions)) != len(self.dict_actions) or any(role not in ("payer", "payee") or action not in
                ("queried", "cache", "skipped_after_initial", "skipped_confidence") for role, action in self.dict_actions):
            raise ValueError("Invalid enrichment action")
        evidence = dict(self.dict_evidence)
        for role, action in self.dict_actions:
            if (role in self.dict_queried) != (action == "queried"):
                raise ValueError("Enrichment action contradicts queried roles")
            if action == "cache" and (role not in evidence or not evidence[role].valid(self.decision_at)):
                raise ValueError("Cache action requires valid evidence for its role")


@dataclass(frozen=True)
class Reason(ImmutableValue):
    _enums = (("floor", (None, APPROVED, TWO_FA, REJECTED)),)
    code: str
    family: str
    points: int = 0
    floor: str | None = None


@dataclass(frozen=True)
class Decision(ImmutableValue):
    _enums = (("status", (APPROVED, TWO_FA, REJECTED)),)
    status: str
    score: int
    families: tuple[tuple[str, int], ...]
    reasons: tuple[Reason, ...]
    local_score: int
    c_pre: int  # ordinal selection coefficient, never a fraud probability
    general_profile: bool
    payee_profile: bool
    recipient_established: bool
    device_state: str
    night_qualifies: bool
    policy_version: str
