"""Pure history and temporal features shared by all criteria."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo
from .context import Context, Pix, HistoricalAnalysis, ExecutionEvent, REJECTED, TWO_FA, supported_time
from .policy import Policy

TZ = ZoneInfo("America/Sao_Paulo")

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


@dataclass(frozen=True)
class Features:
    context: Context
    history: tuple[HistoricalAnalysis, ...]
    mine: tuple[HistoricalAnalysis, ...]
    mature: tuple[HistoricalAnalysis, ...]
    paid_payee: tuple[HistoricalAnalysis, ...]
    mature_payee: tuple[HistoricalAnalysis, ...]
    general: bool
    payee_profile: bool
    established: bool
    device: str
    trusted: bool
    age: timedelta
    qualifies: bool
    day: tuple[HistoricalAnalysis, ...]
    attempts_sum: int
    exposed: tuple[HistoricalAnalysis, ...]
    ns: datetime | None
    count: int
    receipt_count: int
    sec_valid: bool

    @property
    def p(self):
        return self.context.pix

    @property
    def now(self):
        return self.context.decision_at.astimezone(timezone.utc)

    @property
    def t(self):
        return self.p.transaction_at.astimezone(timezone.utc)

    @property
    def profile_median(self):
        reference = self.mature_payee if self.payee_profile else self.mature if self.general else ()
        return median([h.pix.amount_cents for h in reference]) if reference else None

    @property
    def daily_median(self):
        daily = {}
        for h in self.mature:
            day = h.pix.transaction_at.astimezone(TZ).date()
            daily[day] = daily.get(day, 0) + h.pix.amount_cents
        return median(list(daily.values())) if daily else None

    def structuring_count(self, limit, percent):
        def in_band(amount):
            return percent * limit <= amount * 100 <= limit * 100
        return (1 + sum(in_band(h.pix.amount_cents) for h in self.day)) if in_band(self.p.amount_cents) else 0

    @property
    def incoming(self):
        return tuple(h for h in self.history if h.pix.destination == self.p.destination
                     and self.t - timedelta(hours=24) < h.pix.transaction_at <= self.t)

    @property
    def fan_in_payers(self):
        return len({h.pix.payer for h in self.incoming} | {self.p.payer})

    @property
    def fan_in_cents(self):
        return sum(h.pix.amount_cents for h in self.incoming) + self.p.amount_cents

    @property
    def night_soft_exposure(self):
        return sum(h.pix.amount_cents for h in self.exposed if self.ns is not None
                   and h.pix.transaction_at >= self.ns and h.pix.payee_pf and not h.pix.same_holder)

    @property
    def night_hard_exposure(self):
        return sum(h.pix.amount_cents for h in self.exposed if self.ns is not None
                   and h.pix.transaction_at >= self.ns and h.night_qualifies)

    @property
    def device_day_exposure(self):
        return self.p.amount_cents + sum(h.pix.amount_cents for h in self.exposed
            if h.pix.transaction_at.astimezone(TZ).date() == self.t.astimezone(TZ).date()
            and h.device_state == "unregistered")


def build_features(c: Context, policy: Policy) -> Features:
    p = c.pix
    t, now = p.transaction_at.astimezone(timezone.utc), c.decision_at.astimezone(timezone.utc)
    supported_time(now, "decision_at")
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
    count = 1 + sum(t - timedelta(minutes=policy.velocity_minutes) < h.pix.transaction_at <= t for h in mine)
    receipt_count = 1 + sum(h.pix.payer == p.payer and
        (h.pix.payer, h.pix.id) != (p.payer, p.id) and
        now - timedelta(minutes=policy.velocity_minutes) < h.decision_at <= now for h in c.history)
    return Features(c, tuple(history), tuple(mine), tuple(mature), tuple(paid_payee),
                    tuple(mature_payee), general, payee_profile, established, device,
                    trusted, age, qualifies, tuple(day), attempts_sum, tuple(exposed),
                    ns, count, receipt_count, sec_valid)
