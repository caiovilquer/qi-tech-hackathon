"""Immutable experimental policy parameters, with no file/network access."""
from __future__ import annotations
from dataclasses import dataclass, fields
from hashlib import sha256
import json
import re

FAMILIES = ("Limite", "Velocity", "Montante", "Perfil", "Acesso", "Contraparte")


class PolicyConfigurationError(ValueError):
    """The configured version cannot be evaluated safely."""


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
    rule_parameters: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = ()

    def __post_init__(self):
        from .rules import CRITERIA
        def reject(message):
            raise PolicyConfigurationError(message)
        if not isinstance(self.version, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", self.version) is None:
            reject("Invalid policy version")
        if type(self.family_caps) is not tuple or any(type(x) is not tuple or len(x) != 2 for x in self.family_caps):
            reject("family_caps must contain immutable pairs")
        if (len(self.family_caps) != len(FAMILIES) or set(k for k, _ in self.family_caps) != set(FAMILIES)
                or any(type(v) is not int or v < 0 for _, v in self.family_caps)):
            reject("Exactly one nonnegative cap is required for every family")
        object.__setattr__(self, 'family_caps', tuple((k, dict(self.family_caps)[k]) for k in FAMILIES))
        pairs = ("low_history_cents", "velocity_pf", "velocity_pj", "amount_profile_multiples", "recent_account_days")
        for name in pairs + ("structuring_limits",):
            value = getattr(self, name)
            if (type(value) is not tuple or not value or any(type(v) is not int or v <= 0 for v in value)
                    or any(a >= b for a, b in zip(value, value[1:]))
                    or name in pairs and len(value) != 2):
                reject(f"{name} must contain ordered positive integers")
        for item in fields(self):
            if item.name in {"version", "family_caps", "rule_parameters", *pairs, "structuring_limits"}:
                continue
            value = getattr(self, item.name)
            if type(value) is not int or value < (0 if item.name in {"night_start_hour", "night_end_hour", "structuring_percent", "dict_threshold", "uncertainty_each", "uncertainty_cap"} else 1):
                reject(f"Invalid integer parameter: {item.name}")
        if not self.two_fa_at < self.reject_at or not self.night_soft_cents < self.night_hard_cents:
            reject("Decision/night thresholds must be strictly ordered")
        if not 0 <= self.night_end_hour < self.night_start_hour <= 23:
            reject("Night window must cross midnight")
        if any(not 0 <= getattr(self, name) <= 100 for name in ("structuring_percent", "dict_threshold", "uncertainty_each", "uncertainty_cap")):
            reject("Percent/coefficient parameters must be in [0,100]")
        # UTC years [2,9998] leave at least a year for safe arithmetic.
        if self.history_days > 365 or self.maturity_hours > 365*24 or self.velocity_minutes > 365*24*60:
            reject("Time windows exceed the supported one-year arithmetic margin")
        if self.recent_account_days[-1] > 365 or self.dict_ttl_seconds > 365*24*60*60:
            reject("Freshness windows exceed the supported one-year margin")
        if type(self.rule_parameters) is not tuple:
            reject("Criteria configuration must be immutable")
        if not self.rule_parameters:
            object.__setattr__(self, "rule_parameters", tuple((code, rule.defaults) for code, rule in CRITERIA.items()))
        if (type(self.rule_parameters) is not tuple
                or any(type(item) is not tuple or len(item) != 2 for item in self.rule_parameters)):
            reject("Criteria configuration must be immutable")
        if len(self.rule_parameters) != len(CRITERIA) or {code for code, _ in self.rule_parameters} != set(CRITERIA):
            reject("Exactly one configuration is required for every registered criterion")
        for code, parameters in self.rule_parameters:
            expected = dict(CRITERIA[code].defaults)
            if (type(parameters) is not tuple or any(type(item) is not tuple or len(item) != 2 for item in parameters)
                    or len(parameters) != len(expected) or {k for k, _ in parameters} != set(expected)
                    or any(type(v) is not int or v < 0 for _, v in parameters)):
                reject(f"Invalid criterion parameters: {code}")
        object.__setattr__(self, 'rule_parameters', tuple((code, tuple((key, dict(dict(self.rule_parameters)[code])[key])
            for key, _ in rule.defaults)) for code, rule in CRITERIA.items()))

    def parameters_for(self, code):
        return dict(self.rule_parameters)[code]

    def configuration(self):
        parameters = {}
        for item in fields(self):
            if item.name not in {"version", "rule_parameters"}:
                value = getattr(self, item.name)
                parameters[item.name] = dict(value) if item.name == "family_caps" else list(value) if isinstance(value, tuple) else value
        return {"parameters": parameters, "criteria": {code: dict(values) for code, values in self.rule_parameters}}

    @property
    def fingerprint(self):
        data = {"version": self.version, **self.configuration()}
        return sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
