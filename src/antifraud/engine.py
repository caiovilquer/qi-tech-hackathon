"""Deterministic evaluation with a single local/reputation aggregation pipeline."""
from dataclasses import replace

from .context import Context, Decision, RANK, APPROVED, TWO_FA, REJECTED
from .features import build_features
from .policy import Policy
from .rules import CRITERIA

DEFAULT_POLICY = Policy()
ADVERSE_REPUTATION = ("PAYER_DICT_ADVERSE", "PAYEE_DICT_ADVERSE")


def _families(reasons, policy):
    return tuple((family, min(cap, sum(reason.points for reason in reasons if reason.family == family)))
                 for family, cap in policy.family_caps)


def _reasons(features, policy, phase):
    return tuple(reason for criterion in CRITERIA.values() if criterion.phase == phase
                 for reason in criterion.evaluate(features, policy))


def evaluate(context: Context, policy: Policy = DEFAULT_POLICY) -> Decision:
    features = build_features(context, policy)
    local = _reasons(features, policy, "local")
    local_score = sum(points for _, points in _families(local, policy))
    uncertainty = min(policy.uncertainty_cap, policy.uncertainty_each * sum((
        not features.general_profile, features.device_state == "unknown", not features.recipient_established)))
    c_pre = max(0, 100 - min(100, local_score + uncertainty))
    reasons = local + _reasons(features, policy, "reputation")
    families = _families(reasons, policy)
    score = sum(points for _, points in families)
    status = REJECTED if score >= policy.reject_at else TWO_FA if score >= policy.two_fa_at else APPROVED
    if status == REJECTED and not any(reason.code in ADVERSE_REPUTATION or reason.floor == REJECTED
                                      for reason in reasons):
        ceilings = _reasons(features, policy, "ceiling")
        if ceilings:
            # Score stays auditable; the ceiling is recorded only because it changed the outcome.
            status = TWO_FA
            reasons += ceilings
    status = max([status] + [reason.floor for reason in reasons if reason.floor], key=RANK.get)
    return Decision(status, score, families, reasons, local_score, c_pre, features.general_profile,
                    features.payee_profile, features.recipient_established, features.device_state,
                    features.night_qualifies, policy.version)


def preliminary(context: Context, policy: Policy = DEFAULT_POLICY) -> Decision:
    """Same criteria and features, excluding only reputation evidence (C_pre before any DICT lookup)."""
    return evaluate(replace(context, dict_evidence=(), dict_queried=(), dict_actions=()), policy)
