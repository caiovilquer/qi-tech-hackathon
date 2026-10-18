"""Registered pure criteria. Conditions live in code; point weights have a schema.

Hard rejection and 2FA floors are fixed here, never switches in tenant JSON.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from types import MappingProxyType
from typing import Callable, TYPE_CHECKING

from .context import Reason, REJECTED, TWO_FA
from .features import Features

if TYPE_CHECKING:
    from .policy import Policy


@dataclass(frozen=True)
class Criterion:
    code: str
    family: str
    evaluator: Callable
    defaults: tuple[tuple[str, int], ...] = ()
    phase: str = "local"

    @property
    def parameter_schema(self):
        return {"type": "object", "additionalProperties": False,
                "required": [name for name, _ in self.defaults],
                "properties": {name: {"type": "integer", "minimum": 0} for name, _ in self.defaults}}

    def evaluate(self, features: Features, policy: Policy):
        params = dict(policy.parameters_for(self.code))
        result = self.evaluator(features, policy, params)
        if result is None:
            return ()
        points, floor = result
        return (Reason(self.code, self.family, points, floor),)


def night_soft(features, policy, params):
    pix = features.pix
    if features.night_started_at is not None and pix.payer_pf and pix.payee_pf and not pix.same_holder:
        if features.night_soft_exposure + pix.amount_cents > policy.night_soft_cents:
            return 0, TWO_FA


def night_hard(features, policy, params):
    if features.night_started_at is not None and features.night_qualifies:
        if features.night_hard_exposure + features.pix.amount_cents > policy.night_hard_cents:
            return 0, REJECTED


def low_history(features, policy, params):
    if not features.general_profile and not features.payee_profile:
        factor = 1 if features.pix.payer_pf else policy.pj_factor
        if features.pix.amount_cents > policy.low_history_cents[1] * factor:
            return params["high_points"], None
        if features.pix.amount_cents > policy.low_history_cents[0] * factor:
            return params["low_points"], None


def velocity(features, policy, params, *, receipt=False):
    soft, hard = policy.velocity_pf if features.pix.payer_pf else policy.velocity_pj
    count = features.receipt_count if receipt else features.velocity_count
    if count >= soft:
        points = params["points"]
        if receipt and features.velocity_count >= soft:
            # Both clocks describe one signal, including asymmetric tenant weights.
            points = max(0, points - dict(policy.parameters_for("VELOCITY_BURST"))["points"])
        return points, REJECTED if count >= hard else None


def daily_amount(features, policy, params):
    if len(features.day_attempts) + 1 >= 2:
        threshold = (policy.daily_profile_multiple * features.daily_median if features.general_profile
                     else policy.daily_cents * (1 if features.pix.payer_pf else policy.pj_factor))
        if features.day_amount_cents > threshold:
            return params["points"], None


def structuring(features, policy, params):
    for limit in policy.structuring_limits:
        if features.structuring_count(limit, policy.structuring_percent) >= policy.structuring_count:
            return params["points"], None


def amount_profile(features, policy, params):
    reference = features.profile_median
    if reference is not None:
        if features.pix.amount_cents > policy.amount_profile_multiples[1] * reference:
            return params["high_points"], None
        if features.pix.amount_cents > policy.amount_profile_multiples[0] * reference:
            return params["low_points"], None


def recent_account(features, policy, params):
    if features.destination_age < timedelta(days=policy.recent_account_days[0]):
        return params["young_points"], None
    if features.destination_age < timedelta(days=policy.recent_account_days[1]):
        return params["recent_points"], None


def new_payee(features, policy, params):
    if features.general_profile and not features.paid_payee and not features.pix.same_holder:
        return params["points"], None


def self_transfer(features, policy, params):
    if features.pix.same_holder and features.destination_age < timedelta(days=policy.recent_account_days[0]):
        return params["points"], None


def fan_in(features, policy, params):
    if (features.destination_age < timedelta(days=policy.recent_account_days[1])
            and features.fan_in_payers >= policy.fan_in_payers
            and (features.pix.amount_cents >= policy.fan_in_current_cents
                 or features.fan_in_cents >= policy.fan_in_sum_cents)):
        return params["pf_points"] if features.pix.payee_pf else params["pj_points"], None


def changed_unknown_device(features):
    return features.device_state == "unknown" and any(
        analysis.pix.device_id != features.pix.device_id for analysis in features.payer_history)


def device_revoked(features, policy, params):
    if features.device_state == "revoked":
        return 0, REJECTED


def unregistered(features, policy, params):
    if features.device_state == "unregistered":
        return params["points"], None


def device_change(features, policy, params):
    if changed_unknown_device(features):
        return params["points"], None


def device_limit(features, policy, params):
    if features.device_state == "unregistered" or changed_unknown_device(features):
        if (features.pix.amount_cents > policy.device_single_cents
                or features.device_state == "unregistered" and features.device_day_exposure > policy.device_day_cents):
            return 0, TWO_FA


def recovery(features, policy, params):
    security = features.context.security
    if security is not None and any(features.now - timedelta(hours=24) < occurred <= features.now
                                    and available <= features.now for occurred, available in security.recoveries):
        return params["points"], None


def bank_restricted(features, policy, params, *, role):
    security = features.context.security
    if security is not None and any(restriction.applies(features.pix, role, features.now)
                                    for restriction in security.restrictions):
        return 0, REJECTED


def theft_reported(features, policy, params):
    if features.security_valid and features.context.security.theft_reported:
        return 0, REJECTED


def duress_reported(features, policy, params):
    if features.security_valid and features.context.security.duress_reported:
        return 0, REJECTED


def bank_authorized(features, policy, params):
    if features.security_valid and features.context.security.context_authorized:
        return 0, TWO_FA


def same_holder_ceiling(features, policy, params):
    if features.pix.same_holder:
        return 0, TWO_FA


def bank_limit(features, policy, params):
    if features.security_valid and features.context.security.bank_limit_exceeded:
        return 0, REJECTED


def dict_adverse(features, policy, params, *, role):
    evidence = dict(features.context.dict_evidence)
    item = evidence.get(role)
    if item is not None and item.valid(features.now) and (item.fraud_count >= 1 or item.open_reports >= 1):
        payer = evidence.get("payer")
        duplicate = (role == "payee" and payer is not None and payer.document == item.document
                     and payer.valid(features.now) and (payer.fraud_count >= 1 or payer.open_reports >= 1))
        points = params["points"]
        if duplicate:
            points = max(0, points - dict(policy.parameters_for("PAYER_DICT_ADVERSE"))["points"])
        return points, None


def dict_restricted(features, policy, params, *, role):
    item = dict(features.context.dict_evidence).get(role)
    if item is not None and item.valid(features.now) and any(
            restriction.applies(features.pix, role, features.now) for restriction in item.restrictions):
        return 0, REJECTED


def dict_unavailable(features, policy, params):
    evidence = dict(features.context.dict_evidence)
    if any(role not in evidence or not evidence[role].valid(features.now) for role in features.context.dict_queried):
        return params["points"], None


# Order is part of deterministic auditing; no tenant may remove a criterion.
_ENTRIES = (
    Criterion("NIGHT_SOFT_LIMIT", "Limite", night_soft),
    Criterion("NIGHT_HARD_LIMIT", "Limite", night_hard),
    Criterion("LOW_HISTORY_EXPOSURE", "Limite", low_history, (("low_points", 15), ("high_points", 30))),
    Criterion("VELOCITY_BURST", "Velocity", velocity, (("points", 30),)),
    Criterion("RECEIPT_VELOCITY", "Velocity", partial(velocity, receipt=True), (("points", 30),)),
    Criterion("DAILY_AMOUNT", "Montante", daily_amount, (("points", 20),)),
    Criterion("STRUCTURING", "Montante", structuring, (("points", 30),)),
    Criterion("AMOUNT_ABOVE_PROFILE", "Perfil", amount_profile, (("low_points", 15), ("high_points", 30))),
    Criterion("RECENT_PAYEE_ACCOUNT", "Contraparte", recent_account, (("young_points", 25), ("recent_points", 10))),
    Criterion("NEW_PAYEE", "Contraparte", new_payee, (("points", 10),)),
    Criterion("SELF_TRANSFER_NEW_ACCOUNT", "Contraparte", self_transfer, (("points", 15),)),
    Criterion("MULE_FAN_IN", "Contraparte", fan_in, (("pf_points", 40), ("pj_points", 20))),
    Criterion("DEVICE_REVOKED", "Acesso", device_revoked),
    Criterion("UNREGISTERED_DEVICE", "Acesso", unregistered, (("points", 20),)),
    Criterion("DEVICE_CHANGE", "Acesso", device_change, (("points", 20),)),
    Criterion("DEVICE_POLICY_LIMIT", "Acesso", device_limit),
    Criterion("RECENT_ACCOUNT_RECOVERY", "Acesso", recovery, (("points", 25),)),
    Criterion("DEVICE_THEFT_REPORTED", "Acesso", theft_reported),
    Criterion("DURESS_REPORTED", "Acesso", duress_reported),
    Criterion("PAYER_BANK_RESTRICTED", "Contraparte", partial(bank_restricted, role="payer")),
    Criterion("PAYEE_BANK_RESTRICTED", "Contraparte", partial(bank_restricted, role="payee")),
    Criterion("BANK_EFFECTIVE_LIMIT", "Limite", bank_limit),
    Criterion("PAYER_DICT_ADVERSE", "Contraparte", partial(dict_adverse, role="payer"), (("points", 30),), "reputation"),
    Criterion("PAYER_DICT_RESTRICTED", "Contraparte", partial(dict_restricted, role="payer"), (), "reputation"),
    Criterion("PAYEE_DICT_ADVERSE", "Contraparte", partial(dict_adverse, role="payee"), (("points", 30),), "reputation"),
    Criterion("PAYEE_DICT_RESTRICTED", "Contraparte", partial(dict_restricted, role="payee"), (), "reputation"),
    Criterion("DICT_CHECK_UNAVAILABLE", "Contraparte", dict_unavailable, (("points", 10),), "reputation"),
    # Ceilings: a score-only rejection becomes 2FA. Never applied over a rejection floor
    # or adverse reputation, and recorded only when it changed the outcome.
    Criterion("BANK_AUTHORIZED_CONTEXT", "Acesso", bank_authorized, (), "ceiling"),
    Criterion("SAME_HOLDER_TRANSFER", "Contraparte", same_holder_ceiling, (), "ceiling"),
)
CRITERIA = MappingProxyType({entry.code: entry for entry in _ENTRIES})
