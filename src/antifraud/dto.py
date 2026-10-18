"""Frozen public response and private audit record, without persistence or I/O."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
from uuid import UUID

from antifraud.adapter import to_pix
from antifraud.context import Context, Decision
from antifraud.contract import NormalizedPayload
from antifraud.features import build_features
from antifraud.policy import Policy


def _json_default(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Fraction):
        return {"numerator": value.numerator, "denominator": value.denominator}
    raise TypeError(f"Unsupported audit value: {type(value).__name__}")


def _freeze(value):
    return json.dumps(value, default=_json_default, ensure_ascii=True,
                      allow_nan=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class PixTransactionDTO:
    """JSON strings have no mutable backing state. Storage will persist both."""
    public_json: str
    audit_json: str

    @property
    def public(self):
        return json.loads(self.public_json)

    @property
    def audit(self):
        return json.loads(self.audit_json)

    @classmethod
    def from_decision(cls, key: str, normalized: NormalizedPayload, context: Context, policy: Policy, decision: Decision):
        # Key and decision time are supplied by the caller, never a global clock.
        key = str(UUID(key))
        if context.pix != to_pix(normalized, context.pix.payer.tenant):
            raise ValueError("Context Pix does not match the normalized official payload")
        if decision.policy_version != policy.version:
            raise ValueError("Decision policy version does not match the audit policy")
        features = build_features(context, policy)
        evidence = dict(context.dict_evidence)
        actions = dict(context.dict_actions)
        enrichment = {}
        for role in ("payer", "payee"):
            item = evidence.get(role)
            action = actions.get(role)
            if action is None and role in context.dict_queried:
                action = "queried"
            state = item.state if item is not None else "unavailable" if role in context.dict_queried else None
            if item is not None and item.state == "available" and not item.valid(context.decision_at):
                state = "stale"
            enrichment[role] = {"action": action, "state": state}
        public = {"key": key, "status": decision.status, "metadata": {
            "policy_version": policy.version, "score": decision.score,
            "confidence_pre": decision.c_pre, "reasons": [r.code for r in decision.reasons],
            "decision_at": context.decision_at, "data_quality": normalized.data_quality,
            "enrichment": enrichment}}
        audit = {
            "key": key, "tenant": context.pix.payer.tenant, "pix_id": context.pix.id,
            "payload": normalized.canonical_payload, "payload_hash": normalized.payload_hash,
            "invalid_ip_raw": normalized.invalid_ip_raw,
            "data_quality": normalized.data_quality, "decision_at": context.decision_at,
            "policy": {"version": policy.version, "fingerprint": policy.fingerprint, **policy.configuration()},
            "decision": asdict(decision), "security": asdict(context.security) if context.security else None,
            "dict_evidence": {role: asdict(item) for role, item in context.dict_evidence},
            "dict_queried": context.dict_queried, "enrichment": enrichment,
            "provider_traces": tuple(asdict(trace) for trace in context.provider_traces),
            "bank_payments": tuple(asdict(payment) for payment in context.bank_payments),
            "features": {
                "general_profile": features.general, "payee_profile": features.payee_profile,
                "recipient_established": features.established, "device_state": features.device,
                "trusted_beneficiary": features.trusted, "security_valid": features.sec_valid,
                "destination_age_microseconds": features.age // timedelta(microseconds=1),
                "night_qualifies": features.qualifies, "night_start": features.ns,
                "history_count": len(features.history), "payer_history_count": len(features.mine),
                "bank_payment_count": len(features.bank_payments),
                "bank_payment_refs": tuple((p.provider, p.operation_id) for p in features.bank_payments),
                "mature_payments": len(features.mature), "mature_payee_payments": len(features.mature_payee),
                "paid_payee_payments": len(features.paid_payee),
                "velocity_count": features.count, "receipt_count": features.receipt_count,
                "day_attempts": len(features.day) + 1, "day_amount_cents": features.attempts_sum,
                "profile_median_cents": features.profile_median, "daily_median_cents": features.daily_median,
                "structuring_counts": {str(limit): features.structuring_count(limit, policy.structuring_percent)
                                       for limit in policy.structuring_limits},
                "fan_in_payers": features.fan_in_payers, "fan_in_cents": features.fan_in_cents,
                "night_soft_exposure_cents": features.night_soft_exposure,
                "night_hard_exposure_cents": features.night_hard_exposure,
                "device_day_exposure_cents": features.device_day_exposure,
            },
        }
        return cls(_freeze(public), _freeze(audit))
