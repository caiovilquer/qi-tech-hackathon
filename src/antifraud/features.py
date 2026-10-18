"""Pure history and temporal features shared by all criteria."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo
from .context import Context, HistoricalAnalysis, ExecutionEvent, BankPayment, REJECTED, TWO_FA, supported_time
from .policy import Policy

TZ = ZoneInfo("America/Sao_Paulo")
FAN_IN_WINDOW = timedelta(hours=24)  # attempts to one destination that feed MULE_FAN_IN


def median(values: list[int]) -> Fraction:
    ordered = sorted(values)
    count = len(ordered)
    return Fraction(ordered[count // 2]) if count % 2 else Fraction(ordered[count // 2 - 1] + ordered[count // 2], 2)


def night_start(transaction_at: datetime, policy: Policy) -> datetime | None:
    local_time = transaction_at.astimezone(TZ)
    if local_time.hour >= policy.night_start_hour:
        return local_time.replace(hour=policy.night_start_hour, minute=0, second=0, microsecond=0)
    if local_time.hour < policy.night_end_hour:
        return (local_time - timedelta(days=1)).replace(hour=policy.night_start_hour, minute=0, second=0, microsecond=0)
    return None


def latest_execution(analysis: HistoricalAnalysis, context: Context) -> ExecutionEvent | None:
    events = [event for event in context.execution_events
              if event.payer == analysis.pix.payer and event.pix_id == analysis.pix.id
              and event.occurred_at <= context.decision_at and event.available_at <= context.decision_at]
    return max(events, key=lambda event: (event.occurred_at, event.available_at, event.event_id), default=None)


def effective(analysis: HistoricalAnalysis, context: Context) -> bool:
    event = latest_execution(analysis, context)
    return event is not None and event.state == "executed"


def exposure(analysis: HistoricalAnalysis, context: Context) -> bool:
    event = latest_execution(analysis, context)
    if event is not None:
        if event.state in ("failed", "cancelled"):
            return False
        if event.state == "executed":
            return True
    if analysis.status == REJECTED:
        return False
    terminal_failure = any(auth.payer == analysis.pix.payer and auth.pix_id == analysis.pix.id
        and analysis.challenge_id is not None and auth.challenge_id == analysis.challenge_id
        and auth.state == "failed" and auth.terminal and auth.occurred_at <= context.decision_at
        and auth.available_at <= context.decision_at for auth in context.auth_events)
    return not (analysis.status == TWO_FA and terminal_failure)


@dataclass(frozen=True)
class Features:
    context: Context
    history: tuple[HistoricalAnalysis, ...]
    payer_history: tuple[HistoricalAnalysis, ...]
    mature: tuple[HistoricalAnalysis | BankPayment, ...]
    paid_payee: tuple[HistoricalAnalysis | BankPayment, ...]
    mature_payee: tuple[HistoricalAnalysis | BankPayment, ...]
    general_profile: bool
    payee_profile: bool
    recipient_established: bool
    device_state: str
    trusted_beneficiary: bool
    destination_age: timedelta
    night_qualifies: bool
    day_attempts: tuple[HistoricalAnalysis, ...]
    day_amount_cents: int
    exposures: tuple[HistoricalAnalysis, ...]
    night_started_at: datetime | None
    velocity_count: int
    receipt_count: int
    security_valid: bool
    bank_payments: tuple[BankPayment, ...]

    @property
    def pix(self):
        return self.context.pix

    @property
    def now(self):
        return self.context.decision_at.astimezone(timezone.utc)

    @property
    def transaction_at(self):
        return self.pix.transaction_at.astimezone(timezone.utc)

    @property
    def profile_median(self):
        reference = self.mature_payee if self.payee_profile else self.mature if self.general_profile else ()
        return median([payment.pix.amount_cents for payment in reference]) if reference else None

    @property
    def daily_median(self):
        daily_totals = {}
        for payment in self.mature:
            day = payment.pix.transaction_at.astimezone(TZ).date()
            daily_totals[day] = daily_totals.get(day, 0) + payment.pix.amount_cents
        return median(list(daily_totals.values())) if daily_totals else None

    def structuring_count(self, limit, percent):
        def in_band(amount):
            return percent * limit <= amount * 100 <= limit * 100
        if not in_band(self.pix.amount_cents):
            return 0
        return 1 + sum(in_band(attempt.pix.amount_cents) for attempt in self.day_attempts)

    @property
    def incoming(self):
        return tuple(analysis for analysis in self.history if analysis.pix.destination == self.pix.destination
                     and self.transaction_at - FAN_IN_WINDOW < analysis.pix.transaction_at <= self.transaction_at)

    @property
    def fan_in_payers(self):
        return len({analysis.pix.payer for analysis in self.incoming} | {self.pix.payer})

    @property
    def fan_in_cents(self):
        return sum(analysis.pix.amount_cents for analysis in self.incoming) + self.pix.amount_cents

    @property
    def night_soft_exposure(self):
        return sum(analysis.pix.amount_cents for analysis in self.exposures if self.night_started_at is not None
                   and analysis.pix.transaction_at >= self.night_started_at and analysis.pix.payee_pf
                   and not analysis.pix.same_holder)

    @property
    def night_hard_exposure(self):
        return sum(analysis.pix.amount_cents for analysis in self.exposures if self.night_started_at is not None
                   and analysis.pix.transaction_at >= self.night_started_at and analysis.night_qualifies)

    @property
    def device_day_exposure(self):
        local_day = self.transaction_at.astimezone(TZ).date()
        return self.pix.amount_cents + sum(analysis.pix.amount_cents for analysis in self.exposures
            if analysis.pix.transaction_at.astimezone(TZ).date() == local_day and analysis.device_state == "unregistered")


def build_features(context: Context, policy: Policy) -> Features:
    pix = context.pix
    transaction_at = pix.transaction_at.astimezone(timezone.utc)
    now = context.decision_at.astimezone(timezone.utc)
    supported_time(now, "decision_at")
    history = [analysis for analysis in context.history if analysis.pix.payer.tenant == pix.payer.tenant
               and analysis.decision_at <= now and analysis.pix.transaction_at <= transaction_at
               and analysis.pix.transaction_at > transaction_at - timedelta(days=policy.history_days)
               and (analysis.pix.payer, analysis.pix.id) != (pix.payer, pix.id)]
    payer_history = [analysis for analysis in history if analysis.pix.payer == pix.payer]
    executed = [analysis for analysis in payer_history if effective(analysis, context)]
    imported = [payment for payment in context.bank_payments if payment.pix.payer == pix.payer
                and payment.pix.id != pix.id
                and transaction_at - timedelta(days=policy.history_days) < payment.pix.transaction_at <= transaction_at
                and payment.execution.occurred_at <= now and payment.execution.available_at <= now]
    maturity = transaction_at - timedelta(hours=policy.maturity_hours)
    mature = [analysis for analysis in executed if analysis.pix.transaction_at <= maturity
              and latest_execution(analysis, context).occurred_at <= maturity]
    mature.extend(payment for payment in imported
                  if payment.pix.transaction_at <= maturity and payment.execution.occurred_at <= maturity)
    general_profile = (len(mature) >= policy.general_min_payments and
                       len({payment.pix.transaction_at.astimezone(TZ).date() for payment in mature}) >= policy.general_min_days)
    paid_payee = [payment for payment in executed + imported if payment.pix.destination == pix.destination]
    mature_payee = [payment for payment in mature if payment.pix.destination == pix.destination]
    payee_profile = len(mature_payee) >= policy.payee_min_payments
    recipient_established = bool(mature_payee)
    security = context.security
    security_valid = security is not None and security.valid(now)
    device_state = security.device_state if security_valid else "unknown"
    trusted_beneficiary = bool(security_valid and security.trusted_beneficiary)
    destination_age = transaction_at - pix.destination_opened_at
    night_qualifies = pix.payer_pf and ((pix.payee_pf and not pix.same_holder and not trusted_beneficiary)
                                        or (pix.same_holder and destination_age < timedelta(days=policy.recent_account_days[0])))
    day_attempts = [analysis for analysis in payer_history
                    if transaction_at - timedelta(hours=24) < analysis.pix.transaction_at <= transaction_at]
    day_amount_cents = sum(analysis.pix.amount_cents for analysis in day_attempts) + pix.amount_cents
    exposures = [analysis for analysis in payer_history if exposure(analysis, context)]
    night_started_at = night_start(transaction_at, policy)
    velocity_count = 1 + sum(transaction_at - timedelta(minutes=policy.velocity_minutes) < analysis.pix.transaction_at <= transaction_at
                             for analysis in payer_history)
    receipt_count = 1 + sum(analysis.pix.payer == pix.payer and (analysis.pix.payer, analysis.pix.id) != (pix.payer, pix.id)
                            and now - timedelta(minutes=policy.velocity_minutes) < analysis.decision_at <= now
                            for analysis in context.history)
    return Features(context, tuple(history), tuple(payer_history), tuple(mature), tuple(paid_payee), tuple(mature_payee),
                    general_profile, payee_profile, recipient_established, device_state, trusted_beneficiary,
                    destination_age, night_qualifies, tuple(day_attempts), day_amount_cents, tuple(exposures),
                    night_started_at, velocity_count, receipt_count, security_valid, tuple(imported))
