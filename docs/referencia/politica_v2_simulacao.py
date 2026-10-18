"""Referência experimental V3, stdlib; nome legado preservado para reprodução.

Não implementa API/banco, concorrência, DICT real, eficácia ou conformidade BCB.
`evaluate(Context, Policy)` é puro. `Simulator` orquestra fixtures, cache,
idempotência e snapshots em memória. As consultas históricas das fixtures NÃO
existem como operação arbitrária do DICT real. Todos os valores são centavos.
Uso: python3 politica_v2_simulacao.py; python3 -m unittest -v test_politica_pix.py
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Sao_Paulo")
APPROVED, TWO_FA, REJECTED = "approved", "two_factor_required", "rejected"
RANK = {APPROVED: 0, TWO_FA: 1, REJECTED: 2}


def normalized_document(value: str) -> str:
    """Boundary helper: normalize presentation, not registry/DV validation."""
    return re.sub(r"[.\-/\s]", "", value).upper()


def cents(value: str) -> int:
    """Scenario helper: text only, with no float conversion or rounding."""
    if not isinstance(value, str):
        raise TypeError("Use integer cents or a decimal string")
    result = Decimal(value) * 100
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError("Amount must have at most two decimal places")
    return int(result)


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


@dataclass(frozen=True)
class AccountKey:
    tenant: str
    branch: str
    account: str
    document: str


@dataclass(frozen=True)
class DestinationKey:
    participant: str
    branch: str
    account: str


@dataclass(frozen=True)
class Pix:
    id: str
    payer: AccountKey
    payee_document: str
    destination: DestinationKey
    amount_cents: int
    transaction_at: datetime
    destination_opened_at: datetime
    device_id: str = "dev-1"

    def __post_init__(self) -> None:
        if type(self.amount_cents) is not int or self.amount_cents <= 0:
            raise ValueError("amount_cents must be a positive integer")
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
class ExecutionEvent:
    event_id: str
    payer: AccountKey
    pix_id: str
    state: str  # executed | failed | cancelled; trusted bank fact
    occurred_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class AuthEvent:
    event_id: str
    payer: AccountKey
    pix_id: str
    challenge_id: str
    state: str  # passed | failed
    terminal: bool
    occurred_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class Restriction:
    document: str
    role: str  # payer | payee | both
    observed_at: datetime
    available_at: datetime
    valid_until: datetime
    active: bool = True
    account: AccountKey | DestinationKey | None = None

    def applies(self, pix: Pix, role: str, now: datetime) -> bool:
        account = pix.payer if role == "payer" else pix.destination
        document = pix.payer.document if role == "payer" else pix.payee_document
        return (self.active and self.role in (role, "both") and self.document == document
                and self.account in (None, account) and self.observed_at <= now
                and self.available_at <= now < self.valid_until)


@dataclass(frozen=True)
class SecuritySnapshot:
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
class DictEvidence:
    document: str
    state: str  # available | not_covered | unavailable | stale
    observed_at: datetime
    available_at: datetime
    valid_until: datetime
    fraud_count: int = 0
    open_reports: int = 0
    restrictions: tuple[Restriction, ...] = ()
    provenance: str = "synthetic_fixture"
    source_version: int = 0

    def valid(self, now: datetime) -> bool:
        return (self.state == "available" and self.observed_at <= now
                and self.available_at <= now < self.valid_until)


@dataclass(frozen=True)
class HistoricalAnalysis:
    pix: Pix
    decision_at: datetime
    sequence: int
    status: str
    challenge_id: str | None = None
    device_state: str = "unknown"
    night_qualifies: bool = False  # stored designation at original decision


@dataclass(frozen=True)
class Context:
    pix: Pix
    decision_at: datetime
    history: tuple[HistoricalAnalysis, ...] = ()
    execution_events: tuple[ExecutionEvent, ...] = ()
    auth_events: tuple[AuthEvent, ...] = ()
    security: SecuritySnapshot | None = None
    dict_evidence: tuple[tuple[str, DictEvidence], ...] = ()  # (payer/payee, evidence)
    dict_queried: tuple[str, ...] = ()
    dict_actions: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Policy:
    version: str = "pix-experimental-v3"
    family_caps: tuple[tuple[str, int], ...] = (("Limite", 30), ("Velocity", 30),
        ("Montante", 30), ("Perfil", 30), ("Acesso", 40), ("Contraparte", 50))
    two_fa_at: int = 30
    reject_at: int = 70
    history_days: int = 90
    maturity_hours: int = 24
    general_min_payments: int = 5
    general_min_days: int = 3
    payee_min_payments: int = 2
    low_history_cents: tuple[int, int] = (100_000, 500_000)
    pj_factor: int = 5
    velocity_minutes: int = 10
    velocity_pf: tuple[int, int] = (5, 10)
    velocity_pj: tuple[int, int] = (25, 100)
    daily_cents: int = 500_000
    daily_profile_multiple: int = 3
    amount_profile_multiples: tuple[int, int] = (2, 5)
    structuring_limits: tuple[int, ...] = (100_000, 300_000, 500_000)
    structuring_percent: int = 85
    structuring_count: int = 3
    recent_account_days: tuple[int, int] = (7, 30)
    fan_in_payers: int = 5
    fan_in_current_cents: int = 50_000
    fan_in_sum_cents: int = 500_000
    device_single_cents: int = 20_000
    device_day_cents: int = 100_000
    night_soft_cents: int = 100_000
    night_hard_cents: int = 300_000
    night_start_hour: int = 20
    night_end_hour: int = 6
    dict_threshold: int = 80
    uncertainty_each: int = 20
    uncertainty_cap: int = 40
    dict_ttl_seconds: int = 300


@dataclass(frozen=True)
class Reason:
    code: str
    family: str
    points: int = 0
    floor: str | None = None


@dataclass(frozen=True)
class Decision:
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


def median(values: list[int]) -> Fraction:
    ordered = sorted(values)
    n = len(ordered)
    return Fraction(ordered[n // 2]) if n % 2 else Fraction(ordered[n // 2 - 1] + ordered[n // 2], 2)


def night_start(t: datetime, policy: Policy) -> datetime | None:
    t = t.astimezone(TZ)
    if t.hour >= policy.night_start_hour:
        return t.replace(hour=policy.night_start_hour, minute=0, second=0, microsecond=0)
    if t.hour < policy.night_end_hour:
        return (t - timedelta(days=1)).replace(hour=policy.night_start_hour, minute=0, second=0, microsecond=0)
    return None


def latest_execution(h: HistoricalAnalysis, c: Context) -> ExecutionEvent | None:
    events = [e for e in c.execution_events if e.payer == h.pix.payer and e.pix_id == h.pix.id
              and e.occurred_at <= c.decision_at and e.available_at <= c.decision_at]
    return max(events, key=lambda e: (e.occurred_at, e.available_at, e.event_id), default=None)


def effective(h: HistoricalAnalysis, c: Context) -> bool:
    event = latest_execution(h, c)
    return event is not None and event.state == "executed"


def exposure(h: HistoricalAnalysis, c: Context) -> bool:
    event = latest_execution(h, c)
    if event is not None:
        if event.state in ("failed", "cancelled"):
            return False
        if event.state == "executed":
            return True
    if h.status == REJECTED:
        return False
    terminal_failure = any(e.payer == h.pix.payer and e.pix_id == h.pix.id
        and h.challenge_id is not None and e.challenge_id == h.challenge_id
        and e.state == "failed" and e.terminal and e.occurred_at <= c.decision_at
        and e.available_at <= c.decision_at for e in c.auth_events)
    return not (h.status == TWO_FA and terminal_failure)


def evaluate(c: Context, policy: Policy = Policy()) -> Decision:
    """Pure decision over an immutable snapshot; no clock, provider or I/O."""
    p, t, now = c.pix, c.pix.transaction_at, c.decision_at
    supported_time(now, "decision_at")
    hits: list[Reason] = []
    caps = dict(policy.family_caps)

    def hit(code: str, family: str, points: int = 0, floor: str | None = None) -> None:
        hits.append(Reason(code, family, points, floor))

    def families() -> tuple[tuple[str, int], ...]:
        return tuple((family, min(cap, sum(h.points for h in hits if h.family == family)))
                     for family, cap in caps.items())

    history = [h for h in c.history if h.pix.payer.tenant == p.payer.tenant
               and h.decision_at <= now and h.pix.transaction_at <= t
               and h.pix.transaction_at > t - timedelta(days=policy.history_days)
               and (h.pix.payer, h.pix.id) != (p.payer, p.id)]
    mine = [h for h in history if h.pix.payer == p.payer]
    executed = [h for h in mine if effective(h, c)]
    maturity = t - timedelta(hours=policy.maturity_hours)
    mature = [h for h in executed if h.pix.transaction_at <= maturity
              and latest_execution(h, c).occurred_at <= maturity]
    general = (len(mature) >= policy.general_min_payments and
               len({h.pix.transaction_at.astimezone(TZ).date() for h in mature}) >= policy.general_min_days)
    paid_payee = [h for h in executed if h.pix.destination == p.destination]
    mature_payee = [h for h in mature if h.pix.destination == p.destination]
    payee_profile = len(mature_payee) >= policy.payee_min_payments
    established = bool(mature_payee)
    sec = c.security
    sec_valid = sec is not None and sec.valid(now)
    device = sec.device_state if sec_valid else "unknown"
    trusted = bool(sec_valid and sec.trusted_beneficiary)
    age = t - p.destination_opened_at
    qualifies = p.payer_pf and ((p.payee_pf and not p.same_holder and not trusted)
                               or (p.same_holder and age < timedelta(days=policy.recent_account_days[0])))
    day = [h for h in mine if t - timedelta(hours=24) < h.pix.transaction_at <= t]
    attempts_sum = sum(h.pix.amount_cents for h in day) + p.amount_cents
    exposed = [h for h in mine if exposure(h, c)]
    ns = night_start(t, policy)
    if ns is not None and p.payer_pf:
        night = [h for h in exposed if h.pix.transaction_at >= ns]
        pf_total = sum(h.pix.amount_cents for h in night if h.pix.payee_pf and not h.pix.same_holder)
        if p.payee_pf and not p.same_holder and pf_total + p.amount_cents > policy.night_soft_cents:
            hit("NIGHT_SOFT_LIMIT", "Limite", floor=TWO_FA)
        q_total = sum(h.pix.amount_cents for h in night if h.night_qualifies)
        if qualifies and q_total + p.amount_cents > policy.night_hard_cents:
            hit("NIGHT_HARD_LIMIT", "Limite", floor=REJECTED)
    if not general and not payee_profile:
        factor = 1 if p.payer_pf else policy.pj_factor
        if p.amount_cents > policy.low_history_cents[1] * factor:
            hit("LOW_HISTORY_EXPOSURE", "Limite", 30)
        elif p.amount_cents > policy.low_history_cents[0] * factor:
            hit("LOW_HISTORY_EXPOSURE", "Limite", 15)
    count = 1 + sum(t - timedelta(minutes=policy.velocity_minutes) < h.pix.transaction_at <= t for h in mine)
    soft, hard = policy.velocity_pf if p.payer_pf else policy.velocity_pj
    receipt_count = 1 + sum(h.pix.payer == p.payer and
        (h.pix.payer, h.pix.id) != (p.payer, p.id) and
        now - timedelta(minutes=policy.velocity_minutes) < h.decision_at <= now for h in c.history)
    if count >= soft:
        hit("VELOCITY_BURST", "Velocity", 30, REJECTED if count >= hard else None)
    if receipt_count >= soft:
        # Two independent clocks, one contribution: MAX, never 30 + 30.
        hit("RECEIPT_VELOCITY", "Velocity", 0 if count >= soft else 30,
            REJECTED if receipt_count >= hard else None)
    if len(day) + 1 >= 2:
        daily: dict[str, int] = {}
        for h in mature:
            key = h.pix.transaction_at.astimezone(TZ).date().isoformat()
            daily[key] = daily.get(key, 0) + h.pix.amount_cents
        threshold = (policy.daily_profile_multiple * median(list(daily.values())) if general
                     else policy.daily_cents * (1 if p.payer_pf else policy.pj_factor))
        if attempts_sum > threshold:
            hit("DAILY_AMOUNT", "Montante", 20)
    for limit in policy.structuring_limits:
        in_band = lambda value: policy.structuring_percent * limit <= value * 100 <= limit * 100
        if in_band(p.amount_cents) and 1 + sum(in_band(h.pix.amount_cents) for h in day) >= policy.structuring_count:
            hit("STRUCTURING", "Montante", 30)
            break
    reference = mature_payee if payee_profile else mature if general else []
    if reference:
        ref = median([h.pix.amount_cents for h in reference])
        if p.amount_cents > policy.amount_profile_multiples[1] * ref:
            hit("AMOUNT_ABOVE_PROFILE", "Perfil", 30)
        elif p.amount_cents > policy.amount_profile_multiples[0] * ref:
            hit("AMOUNT_ABOVE_PROFILE", "Perfil", 15)
    if age < timedelta(days=policy.recent_account_days[0]):
        hit("RECENT_PAYEE_ACCOUNT", "Contraparte", 25)
    elif age < timedelta(days=policy.recent_account_days[1]):
        hit("RECENT_PAYEE_ACCOUNT", "Contraparte", 10)
    if general and not paid_payee and not p.same_holder:
        hit("NEW_PAYEE", "Contraparte", 10)
    if p.same_holder and age < timedelta(days=policy.recent_account_days[0]):
        hit("SELF_TRANSFER_NEW_ACCOUNT", "Contraparte", 15)
    inbound = [h for h in history if h.pix.destination == p.destination
               and t - timedelta(hours=24) < h.pix.transaction_at <= t]
    if (age < timedelta(days=policy.recent_account_days[1]) and
        len({h.pix.payer for h in inbound} | {p.payer}) >= policy.fan_in_payers and
        (p.amount_cents >= policy.fan_in_current_cents or
         sum(h.pix.amount_cents for h in inbound) + p.amount_cents >= policy.fan_in_sum_cents)):
        hit("MULE_FAN_IN", "Contraparte", 40 if p.payee_pf else 20)
    if device == "revoked":
        hit("DEVICE_REVOKED", "Acesso", floor=REJECTED)
    changed_unknown = device == "unknown" and any(h.pix.device_id != p.device_id for h in mine)
    if device == "unregistered" or changed_unknown:
        hit("UNREGISTERED_DEVICE" if device == "unregistered" else "DEVICE_CHANGE", "Acesso", 20)
        today = t.astimezone(TZ).date()
        device_total = p.amount_cents + sum(h.pix.amount_cents for h in exposed
            if h.pix.transaction_at.astimezone(TZ).date() == today and h.device_state == "unregistered")
        if p.amount_cents > policy.device_single_cents or (device == "unregistered" and device_total > policy.device_day_cents):
            hit("DEVICE_POLICY_LIMIT", "Acesso", floor=TWO_FA)
    if sec is not None:
        if any(now - timedelta(hours=24) < occurred <= now and available <= now
               for occurred, available in sec.recoveries):
            hit("RECENT_ACCOUNT_RECOVERY", "Acesso", 25)
        # Restriction validity is its own evidence; the device snapshot TTL cannot hide it.
        for role in ("payer", "payee"):
            if any(r.applies(p, role, now) for r in sec.restrictions):
                hit(f"{role.upper()}_BANK_RESTRICTED", "Contraparte", floor=REJECTED)
    if sec_valid and sec.bank_limit_exceeded:
        hit("BANK_EFFECTIVE_LIMIT", "Limite", floor=REJECTED)
    local_score = sum(v for _, v in families())
    uncertainty = min(policy.uncertainty_cap, policy.uncertainty_each *
                      sum((not general, device == "unknown", not established)))
    c_pre = max(0, 100 - min(100, local_score + uncertainty))
    unavailable = False
    adverse_documents: set[str] = set()
    for role, evidence in c.dict_evidence:
        document = p.payer.document if role == "payer" else p.payee_document
        if evidence.document != document:
            raise ValueError("DICT subject does not match the role")
        if not evidence.valid(now):
            unavailable |= role in c.dict_queried
            continue
        if evidence.fraud_count >= 1 or evidence.open_reports >= 1:
            hit(f"{role.upper()}_DICT_ADVERSE", "Contraparte", 0 if document in adverse_documents else 30)
            adverse_documents.add(document)
        if any(r.applies(p, role, now) for r in evidence.restrictions):
            hit(f"{role.upper()}_DICT_RESTRICTED", "Contraparte", floor=REJECTED)
    if unavailable:
        hit("DICT_CHECK_UNAVAILABLE", "Contraparte", 10)
    fam = families()
    score = sum(v for _, v in fam)
    status = REJECTED if score >= policy.reject_at else TWO_FA if score >= policy.two_fa_at else APPROVED
    status = max([status] + [h.floor for h in hits if h.floor], key=RANK.get)
    return Decision(status, score, fam, tuple(hits), local_score, c_pre, general,
                    payee_profile, established, device, qualifies, policy.version)


class FixtureProviders:
    """Temporal fixtures only; counters are supplied aggregates, not fake DICT windows.

    Security fixtures are scoped to account + device + destination. A real adapter
    must deliver applicable current bank context, independent of DICT call gating.
    observed_at/source_version order facts; available_at only gates knowledge.
    Document fixtures intentionally describe a shared synthetic DICT universe.
    Simulator cache is tenant-scoped; real adapters also scope credentials/context.
    """
    def __init__(self, ttl_seconds: int = 300) -> None:
        self.ttl_seconds = ttl_seconds
        self.dict_fixtures: dict[str, list[DictEvidence]] = {}
        self.security_fixtures: dict[tuple[AccountKey, str, DestinationKey], list[SecuritySnapshot]] = {}
        self.calls: list[tuple[str, str, datetime]] = []
        self.security_calls = 0
        self.down = False

    def lookup_dict(self, document: str, asof: datetime, role: str) -> DictEvidence:
        self.calls.append((role, document, asof))
        if self.down:
            return DictEvidence(document, "unavailable", asof, asof, asof + timedelta(seconds=self.ttl_seconds))
        known = [e for e in self.dict_fixtures.get(document, []) if e.available_at <= asof and e.observed_at <= asof]
        if known:
            result = max(known, key=lambda e: (e.observed_at, e.source_version))
            return result if result.valid(asof) else replace(result, state="stale")
        return DictEvidence(document, "not_covered", asof, asof, asof + timedelta(seconds=self.ttl_seconds))

    def lookup_security(self, pix: Pix, asof: datetime) -> SecuritySnapshot:
        self.security_calls += 1
        key = (pix.payer, pix.device_id, pix.destination)
        known = [s for s in self.security_fixtures.get(key, []) if s.available_at <= asof and s.observed_at <= asof]
        if known:
            return max(known, key=lambda s: (s.observed_at, s.source_version))
        return SecuritySnapshot(asof, asof, asof + timedelta(seconds=self.ttl_seconds))


@dataclass(frozen=True)
class Analysis:
    request_hash: str
    sequence: int
    context: Context
    decision: Decision
    challenge_id: str | None


class IdempotencyConflict(ValueError):
    pass


def json_value(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def request_hash(pix: Pix) -> str:
    """Hash of normalized typed input; API adapter must also cover all POST fields.

    Fixtures have no geolocation/IP fields. Real HTTP canonicalization belongs to
    the controller; the public request hash must not be limited to this subset.
    """
    encoded = json.dumps(asdict(pix), default=json_value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class Simulator:
    """Single-thread demonstration, not a transactional repository.

    API needs per-account lock + unique tenant/id reservation + transaction. This
    class deliberately makes no concurrency or durability claim.
    """
    def __init__(self, providers: FixtureProviders | None = None, policy: Policy = Policy()) -> None:
        self.policy = policy
        self.providers = providers or FixtureProviders(policy.dict_ttl_seconds)
        self.analyses: dict[tuple[str, str], Analysis] = {}
        self.history: list[HistoricalAnalysis] = []
        self.executions: list[ExecutionEvent] = []
        self.auth_events: list[AuthEvent] = []
        self.source_initial: dict[AccountKey, tuple[datetime, DictEvidence | None]] = {}
        self.cache: dict[tuple[str, str], DictEvidence] = {}

    def _event_operation(self, event: ExecutionEvent | AuthEvent) -> HistoricalAnalysis:
        aware(event.occurred_at)
        aware(event.available_at)
        if event.available_at < event.occurred_at:
            raise ValueError("Event cannot be available before it occurred")
        operation = next((h for h in self.history if h.pix.payer == event.payer and h.pix.id == event.pix_id), None)
        if operation is None:
            raise ValueError("Event must reference an analyzed operation or explicit bank history")
        if event.occurred_at < operation.pix.transaction_at:
            raise ValueError("Event precedes the payment intention")
        return operation

    @staticmethod
    def _event_replay(event, events) -> bool:
        existing = next((e for e in events if (e.payer.tenant, e.event_id) == (event.payer.tenant, event.event_id)), None)
        if existing is not None and existing != event:
            raise IdempotencyConflict("Same tenant/event_id with conflicting event content")
        return existing is not None

    def record_execution(self, event: ExecutionEvent) -> None:
        if self._event_replay(event, self.executions):
            return
        self._event_operation(event)
        if event.state not in ("executed", "failed", "cancelled"):
            raise ValueError("Invalid execution state")
        prior = [e for e in self.executions if e.payer == event.payer and e.pix_id == event.pix_id]
        if any(e.state != event.state or e.occurred_at != event.occurred_at for e in prior):
            raise ValueError("Execution is terminal; reversal is a separate financial fact")
        self.executions.append(event)

    def record_auth(self, event: AuthEvent) -> None:
        if self._event_replay(event, self.auth_events):
            return
        operation = self._event_operation(event)
        if operation.challenge_id is None or event.challenge_id != operation.challenge_id:
            raise ValueError("Authentication must reference this operation's exact challenge")
        if event.occurred_at < operation.decision_at:
            raise ValueError("Authentication precedes challenge creation")
        if event.state not in ("passed", "failed") or (event.state == "passed" and not event.terminal):
            raise ValueError("Invalid authentication state; passed is terminal")
        prior = [e for e in self.auth_events if e.payer == event.payer and e.pix_id == event.pix_id
                 and e.challenge_id == event.challenge_id]
        for other in prior:
            if other.terminal and event.terminal and (other.state != event.state or other.occurred_at != event.occurred_at):
                raise ValueError("Contradictory terminal authentication events")
            terminal, attempt = (other, event) if other.terminal else (event, other)
            if terminal.terminal and not attempt.terminal and attempt.occurred_at >= terminal.occurred_at:
                raise ValueError("An authentication attempt cannot follow a terminal outcome")
        self.auth_events.append(event)

    def analyze_payload(self, payload: dict, decision_at: datetime, tenant: str = "demo") -> Analysis:
        """Optional official-contract adapter; requires contrato_pix/jsonschema.

        Canonical payload contributes ALL fields to the immutable replay hash.
        The pure engine continues to receive only the domain facts it uses.
        """
        from contrato_pix import normalize_payload
        normalized = normalize_payload(payload)
        body = normalized.canonical_payload
        source, destination, access = body["source_account"], body["destination_account"], body["source"]
        parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
        pix = Pix(body["id"], AccountKey(tenant, source["branch"], source["account_number"],
                  source["owner"]["document_number"]), destination["owner"]["document_number"],
                  DestinationKey(destination["participant"], destination["branch"], destination["account_number"]),
                  body["amount"], parse(body["transaction_date"]), parse(destination["opening_date"]), access["device_id"])
        return self.analyze(pix, decision_at, payload_hash=normalized.payload_hash)

    def analyze(self, pix: Pix, decision_at: datetime, payload_hash: str | None = None) -> Analysis:
        supported_time(decision_at, "decision_at")
        key, digest = (pix.payer.tenant, pix.id), payload_hash or request_hash(pix)
        previous = self.analyses.get(key)
        if previous is not None:
            if previous.request_hash != digest:
                raise IdempotencyConflict("Same tenant/id with different normalized content")
            return previous
        sec = self.providers.lookup_security(pix, decision_at)
        base = Context(pix, decision_at, tuple(self.history), tuple(self.executions), tuple(self.auth_events), sec)
        preliminary = evaluate(base, self.policy)
        evidence, queried, actions = [], [], []
        for role, document in (("payer", pix.payer.document), ("payee", pix.payee_document)):
            first_source = role == "payer" and pix.payer not in self.source_initial
            should_query = first_source if role == "payer" else preliminary.c_pre < self.policy.dict_threshold
            cache_key = (pix.payer.tenant, document)
            item = self.cache.get(cache_key)
            if item is not None and item.valid(decision_at):
                actions.append((role, "cache"))
            elif should_query:
                cached = item
                item = self.providers.lookup_dict(document, decision_at, role)
                if (cached is not None and cached.observed_at <= decision_at and cached.available_at <= decision_at
                    and (item.observed_at, item.source_version) < (cached.observed_at, cached.source_version)):
                    # Expiry does not authorize a delayed reply to roll back a newer fact.
                    item = replace(cached, state="stale")
                queried.append(role)
                actions.append((role, "queried"))
                if item.valid(decision_at):
                    self.cache[cache_key] = item
            else:
                item = None
                actions.append((role, "skipped_after_initial" if role == "payer" else "skipped_confidence"))
            if item is not None:
                evidence.append((role, item))
            if first_source:
                self.source_initial[pix.payer] = (decision_at, item)
        context = replace(base, dict_evidence=tuple(evidence), dict_queried=tuple(queried), dict_actions=tuple(actions))
        decision = evaluate(context, self.policy)
        challenge = f"challenge:{pix.payer.tenant}:{pix.id}" if decision.status == TWO_FA else None
        analysis = Analysis(digest, len(self.history) + 1, context, decision, challenge)
        self.analyses[key] = analysis
        self.history.append(HistoricalAnalysis(pix, decision_at, analysis.sequence, decision.status,
                                             challenge, decision.device_state, decision.night_qualifies))
        return analysis


BASE = datetime(2026, 10, 17, 14, tzinfo=TZ)
OLD = datetime(2023, 1, 10, 9, tzinfo=TZ)
A, B, C = "12345678909", "98765432100", "52998224725"
EMPRESA = "11222333000181"


def cpf_fixture(index: int) -> str:
    """Deterministic numeric CPF with valid check digits; synthetic, not identity."""
    digits = [int(c) for c in f"{index + 100000000:09d}"]
    for size in (9, 10):
        remainder = sum(d * w for d, w in zip(digits, range(size + 1, 1, -1))) % 11
        digits.append(0 if remainder < 2 else 11 - remainder)
    return "".join(map(str, digits))


def example_pix(identifier: str, amount: int = 5_000, t: datetime = BASE,
                payer: str = A, payee: str = B, account: str | None = None,
                source: str = "88213", opened: datetime = OLD, device: str = "dev-1") -> Pix:
    return Pix(identifier, AccountKey("demo", "0001", source, normalized_document(payer)),
               normalized_document(payee), DestinationKey("12345678", "0001", account or payee),
               amount, t, opened, device)


def seed_executed(sim: Simulator, pix: Pix) -> None:
    """Trusted synthetic bank history; explicit execution, no invented 2FA link."""
    sim.history.append(HistoricalAnalysis(pix, pix.transaction_at, len(sim.history) + 1, APPROVED,
                                         device_state="registered", night_qualifies=False))
    sim.record_execution(ExecutionEvent("exec:" + pix.id, pix.payer, pix.id, "executed",
                                       pix.transaction_at, pix.transaction_at))


def seed_profile(sim: Simulator, payer: str = A, destination: str = B) -> None:
    for i, value in enumerate((8_000, 15_000, 22_000, 12_000, 30_000, 40_000)):
        seed_executed(sim, example_pix(f"history-{payer}-{i}", value, BASE - timedelta(days=10-i),
                                      payer=payer, payee=destination))


def scenarios() -> list[dict]:
    """Illustrations, not an oracle: independent invariants live in unittest."""
    rows: list[dict] = []
    def send(title: str, sim: Simulator, pix: Pix) -> Analysis:
        a = sim.analyze(pix, pix.transaction_at)
        rows.append({"scenario": title, "id": pix.id, "amount_cents": pix.amount_cents,
                     "decision_at": a.context.decision_at.isoformat(), **asdict(a.decision),
                     "dict_actions": dict(a.context.dict_actions)})
        return a
    sim = Simulator()
    for i in range(3):
        send("Exemplo oficial: IDs distintos no mesmo instante", sim,
             example_pix(f"official-{i}", 150_000, BASE.replace(hour=22, minute=15),
                         opened=BASE.replace(day=10, hour=9)))
    send("Mesmo valor durante o dia", Simulator(), example_pix("day", 150_000, opened=BASE - timedelta(days=7)))
    send("Desconhecido legítimo R$50; cobertura externa desconhecida", Simulator(), example_pix("cold"))
    sim = Simulator(); seed_profile(sim)
    for i, day in enumerate((-60, -30, 0)):
        pix = example_pix(f"rent-{i}", 250_000, BASE + timedelta(days=day), payee=C)
        send("Aluguel com execução bancária explícita", sim, pix)
        sim.record_execution(ExecutionEvent(f"rent-exec-{i}", pix.payer, pix.id, "executed", pix.transaction_at, pix.transaction_at))
    sim = Simulator()
    for i in range(2):
        send("Fracionamento na virada da noite", sim, example_pix(f"midnight-{i}", 90_000,
             BASE.replace(hour=23, minute=50) + timedelta(minutes=20*i)))
    sim = Simulator(); seed_profile(sim)
    send("Aparelho desconhecido e destino recente", sim, example_pix("takeover", 280_000, payee=C,
         opened=BASE-timedelta(days=3), device="new-device"))
    for hour in (14, 23):
        sim = Simulator(); seed_profile(sim)
        send("Conta própria recente", sim, example_pix(f"own-{hour}", 500_000, BASE.replace(hour=hour),
             payee=A, account="70002", opened=BASE-timedelta(days=2)))
    send("Coação candidata: destino antigo às 23h", Simulator(), example_pix("coercion", 500_000, BASE.replace(hour=23)))
    sim = Simulator()
    for i in range(6):
        send("Fan-in de intenções, não recebimentos", sim, example_pix(f"fanin-{i}", 80_000,
             BASE-timedelta(hours=5-i), payer=cpf_fixture(i), opened=BASE-timedelta(days=10)))
    sim = Simulator()
    for i in range(5):
        send("Rateio em conta antiga", sim, example_pix(f"split-{i}", 4_000, payer=cpf_fixture(i)))
    sim = Simulator()
    for i in range(3):
        send("Estruturação: três intenções", sim, example_pix(f"struct-{i}", 99_000, BASE+timedelta(hours=i)))
    sim = Simulator()
    for i in range(10):
        send("Rajada mesmo valor/destino e IDs distintos", sim, example_pix(f"burst-{i}", t=BASE+timedelta(seconds=i)))
    sim = Simulator()
    # Explicit established bank payroll history: five days, two prior salaries per account.
    for i in range(20):
        for j in range(2):
            seed_executed(sim, example_pix(f"salary-old-{i}-{j}", 250_000, BASE-timedelta(days=30+j*30+i%3),
                                         payer=EMPRESA, payee=cpf_fixture(i)))
    for i in range(20):
        send("Folha PJ estabelecida: 20 CPFs com DV válido", sim, example_pix(f"salary-{i}", 250_000,
             BASE+timedelta(seconds=30*i), payer=EMPRESA, payee=cpf_fixture(i)))
    provider = FixtureProviders(); provider.down = True
    send("Indisponibilidade dupla não duplica +10", Simulator(provider), example_pix("outage"))
    return rows


if __name__ == "__main__":
    output = scenarios()
    target = Path(__file__).with_name("politica_simulacao_resultados.json")
    target.write_text(json.dumps({"policy": "pix-experimental-v3", "experimental": True,
        "claims": "fixtures sequenciais; não mede eficácia, integração ou concorrência", "scenarios": output},
        ensure_ascii=False, indent=2), encoding="utf-8")
    for row in output:
        print(f"{row['scenario']}: {row['id']} {row['status']} score={row['score']} C_pre={row['c_pre']}")
    print(f"\n{len(output)} observações gravadas em {target.name}")
