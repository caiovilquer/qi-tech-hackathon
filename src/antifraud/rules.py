"""Registered pure criteria. Conditions live in code; weights have a schema.

Hard rejection and 2FA floors are fixed here, not switches in tenant JSON.
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
                "properties": {name: {"type": "integer", "minimum": 0}
                               for name, _ in self.defaults}}

    def evaluate(self, features: Features, policy: Policy):
        parameters = dict(policy.parameters_for(self.code))
        result = self.evaluator(features, policy, parameters)
        if result is None:
            return ()
        points, floor = result
        return (Reason(self.code, self.family, points, floor),)


def night_soft(f, p, args):
    if f.ns is not None and f.p.payer_pf and f.p.payee_pf and not f.p.same_holder:
        if f.night_soft_exposure + f.p.amount_cents > p.night_soft_cents:
            return 0, TWO_FA


def night_hard(f, p, args):
    if f.ns is not None and f.qualifies:
        if f.night_hard_exposure + f.p.amount_cents > p.night_hard_cents:
            return 0, REJECTED


def low_history(f, p, args):
    if not f.general and not f.payee_profile:
        factor = 1 if f.p.payer_pf else p.pj_factor
        if f.p.amount_cents > p.low_history_cents[1] * factor:
            return args["high_points"], None
        if f.p.amount_cents > p.low_history_cents[0] * factor:
            return args["low_points"], None


def velocity(f, p, args, *, receipt=False):
    soft, hard = p.velocity_pf if f.p.payer_pf else p.velocity_pj
    count = f.receipt_count if receipt else f.count
    if count >= soft:
        points = 0 if receipt and f.count >= soft else args["points"]
        return points, REJECTED if count >= hard else None


def daily_amount(f, p, args):
    if len(f.day) + 1 >= 2:
        threshold = (p.daily_profile_multiple * f.daily_median if f.general
                     else p.daily_cents * (1 if f.p.payer_pf else p.pj_factor))
        if f.attempts_sum > threshold:
            return args["points"], None


def structuring(f, p, args):
    for limit in p.structuring_limits:
        if f.structuring_count(limit, p.structuring_percent) >= p.structuring_count:
            return args["points"], None


def amount_profile(f, p, args):
    amount = f.profile_median
    if amount is not None:
        if f.p.amount_cents > p.amount_profile_multiples[1] * amount:
            return args["high_points"], None
        if f.p.amount_cents > p.amount_profile_multiples[0] * amount:
            return args["low_points"], None


def recent_account(f, p, args):
    if f.age < timedelta(days=p.recent_account_days[0]):
        return args["young_points"], None
    if f.age < timedelta(days=p.recent_account_days[1]):
        return args["recent_points"], None


def new_payee(f, p, args):
    if f.general and not f.paid_payee and not f.p.same_holder:
        return args["points"], None


def self_transfer(f, p, args):
    if f.p.same_holder and f.age < timedelta(days=p.recent_account_days[0]):
        return args["points"], None


def fan_in(f, p, args):
    if (f.age < timedelta(days=p.recent_account_days[1])
            and f.fan_in_payers >= p.fan_in_payers
            and (f.p.amount_cents >= p.fan_in_current_cents
                 or f.fan_in_cents >= p.fan_in_sum_cents)):
        return args["pf_points"] if f.p.payee_pf else args["pj_points"], None


def changed_unknown(f):
    return f.device == "unknown" and any(h.pix.device_id != f.p.device_id for h in f.mine)


def device_revoked(f, p, args):
    if f.device == "revoked":
        return 0, REJECTED


def unregistered(f, p, args):
    if f.device == "unregistered":
        return args["points"], None


def device_change(f, p, args):
    if changed_unknown(f):
        return args["points"], None


def device_limit(f, p, args):
    if f.device == "unregistered" or changed_unknown(f):
        if (f.p.amount_cents > p.device_single_cents
                or f.device == "unregistered" and f.device_day_exposure > p.device_day_cents):
            return 0, TWO_FA


def recovery(f, p, args):
    sec = f.context.security
    if sec is not None and any(f.now - timedelta(hours=24) < occurred <= f.now
                              and available <= f.now for occurred, available in sec.recoveries):
        return args["points"], None


def bank_restricted(f, p, args, *, role):
    sec = f.context.security
    if sec is not None and any(r.applies(f.p, role, f.now) for r in sec.restrictions):
        return 0, REJECTED


def bank_limit(f, p, args):
    if f.sec_valid and f.context.security.bank_limit_exceeded:
        return 0, REJECTED


def dict_adverse(f, p, args, *, role):
    evidence = dict(f.context.dict_evidence)
    item = evidence.get(role)
    if item is not None and item.valid(f.now) and (item.fraud_count >= 1 or item.open_reports >= 1):
        payer = evidence.get("payer")
        duplicate = (role == "payee" and payer is not None and payer.document == item.document
                     and payer.valid(f.now) and (payer.fraud_count >= 1 or payer.open_reports >= 1))
        return 0 if duplicate else args["points"], None


def dict_restricted(f, p, args, *, role):
    item = dict(f.context.dict_evidence).get(role)
    if item is not None and item.valid(f.now) and any(r.applies(f.p, role, f.now) for r in item.restrictions):
        return 0, REJECTED


def dict_unavailable(f, p, args):
    evidence = dict(f.context.dict_evidence)
    if any(role not in evidence or not evidence[role].valid(f.now) for role in f.context.dict_queried):
        return args["points"], None


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
    Criterion("PAYER_BANK_RESTRICTED", "Contraparte", partial(bank_restricted, role="payer")),
    Criterion("PAYEE_BANK_RESTRICTED", "Contraparte", partial(bank_restricted, role="payee")),
    Criterion("BANK_EFFECTIVE_LIMIT", "Limite", bank_limit),
    Criterion("PAYER_DICT_ADVERSE", "Contraparte", partial(dict_adverse, role="payer"), (("points", 30),), "reputation"),
    Criterion("PAYER_DICT_RESTRICTED", "Contraparte", partial(dict_restricted, role="payer"), (), "reputation"),
    Criterion("PAYEE_DICT_ADVERSE", "Contraparte", partial(dict_adverse, role="payee"), (("points", 30),), "reputation"),
    Criterion("PAYEE_DICT_RESTRICTED", "Contraparte", partial(dict_restricted, role="payee"), (), "reputation"),
    Criterion("DICT_CHECK_UNAVAILABLE", "Contraparte", dict_unavailable, (("points", 10),), "reputation"),
)
CRITERIA = MappingProxyType({entry.code: entry for entry in _ENTRIES})
