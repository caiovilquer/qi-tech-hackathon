"""Deterministic evaluation with a single local/reputation aggregation pipeline."""
from .context import Context, Decision, RANK, APPROVED, TWO_FA, REJECTED
from .features import build_features
from .policy import Policy
from .rules import CRITERIA

DEFAULT_POLICY = Policy()


def _families(reasons, policy):
    return tuple((family, min(cap, sum(r.points for r in reasons if r.family == family)))
                 for family, cap in policy.family_caps)


def evaluate(context: Context, policy: Policy = DEFAULT_POLICY) -> Decision:
    f = build_features(context, policy)
    local = tuple(reason for criterion in CRITERIA.values() if criterion.phase == "local"
                  for reason in criterion.evaluate(f, policy))
    local_score = sum(value for _, value in _families(local, policy))
    uncertainty = min(policy.uncertainty_cap, policy.uncertainty_each *
                      sum((not f.general, f.device == "unknown", not f.established)))
    c_pre = max(0, 100 - min(100, local_score + uncertainty))
    reputation = tuple(reason for criterion in CRITERIA.values() if criterion.phase == "reputation"
                       for reason in criterion.evaluate(f, policy))
    reasons = local + reputation
    families = _families(reasons, policy)
    score = sum(value for _, value in families)
    status = REJECTED if score >= policy.reject_at else TWO_FA if score >= policy.two_fa_at else APPROVED
    status = max([status] + [r.floor for r in reasons if r.floor], key=RANK.get)
    return Decision(status, score, families, reasons, local_score, c_pre, f.general,
                    f.payee_profile, f.established, f.device, f.qualifies, policy.version)


def preliminary(context: Context, policy: Policy = DEFAULT_POLICY) -> Decision:
    # Same pure criteria and features, excluding only reputation evidence.
    from dataclasses import replace
    return evaluate(replace(context, dict_evidence=(), dict_queried=(), dict_actions=()), policy)
