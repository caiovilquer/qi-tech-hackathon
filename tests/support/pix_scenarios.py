"""Sequential reference harness for policy invariants, never production storage.

Only context/fixture assembly is reused here. Every decision calls the ported
engine; no API, locks, providers or idempotency guarantees are claimed.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from zoneinfo import ZoneInfo
from antifraud.context import *
from antifraud.policy import Policy
from antifraud.engine import evaluate
TZ = ZoneInfo("America/Sao_Paulo")
def cents(value: str) -> int:
    """Scenario helper: text only, with no float conversion or rounding."""
    if not isinstance(value, str):
        raise TypeError("Use integer cents or a decimal string")
    result = Decimal(value) * 100
    if not result.is_finite() or result != result.to_integral_value():
        raise ValueError("Amount must have at most two decimal places")
    return int(result)


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
            return DictEvidence(document, "unavailable", asof, asof, asof + timedelta(seconds=self.ttl_seconds), provenance="synthetic_fixture")
        known = [e for e in self.dict_fixtures.get(document, []) if e.available_at <= asof and e.observed_at <= asof]
        if known:
            result = max(known, key=lambda e: (e.observed_at, e.source_version))
            return result if result.valid(asof) else replace(result, state="stale")
        return DictEvidence(document, "not_covered", asof, asof, asof + timedelta(seconds=self.ttl_seconds), provenance="synthetic_fixture")

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
    """Conflicting test request."""


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
        from antifraud.contract import normalize_payload
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
