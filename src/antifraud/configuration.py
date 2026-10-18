"""Policy loading at startup. I/O is kept outside the pure engine."""
from dataclasses import dataclass, fields
import json
from pathlib import Path
import re

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from .policy import FAMILIES, Policy, PolicyConfigurationError
from .rules import CRITERIA

DEFAULT_POLICY_PATH = Path(__file__).with_name("policies.json")


def configuration_schema():
    default = Policy()
    properties = {}
    for item in fields(default):
        if item.name in {"version", "rule_parameters"}:
            continue
        value = getattr(default, item.name)
        if item.name == "family_caps":
            properties[item.name] = {"type": "object", "additionalProperties": False,
                "required": list(FAMILIES), "properties": {k: {"type": "integer", "minimum": 0} for k in FAMILIES}}
        elif isinstance(value, tuple):
            properties[item.name] = {"type": "array", "minItems": 1 if item.name == "structuring_limits" else len(value),
                "items": {"type": "integer", "minimum": 1}}
            if item.name != "structuring_limits":
                properties[item.name]["maxItems"] = len(value)
        else:
            properties[item.name] = {"type": "integer"}
    return {"type": "object", "additionalProperties": False, "required": ["policies", "tenants"],
        "properties": {
            "policies": {"type": "object", "minProperties": 1,
                "propertyNames": {"pattern": "^[a-z0-9][a-z0-9._-]{0,63}$"},
                "additionalProperties": {"type": "object", "additionalProperties": False,
                    "required": ["parameters", "criteria"], "properties": {
                        "parameters": {"type": "object", "additionalProperties": False,
                                       "required": list(properties), "properties": properties},
                        "criteria": {"type": "object", "additionalProperties": False,
                            "required": list(CRITERIA), "properties": {
                                code: rule.parameter_schema for code, rule in CRITERIA.items()}}}}},
            "tenants": {"type": "object", "minProperties": 1,
                "propertyNames": {"pattern": "^[a-zA-Z0-9_-]{1,64}$"},
                "additionalProperties": {"type": "string"}}}}


@dataclass(frozen=True)
class PolicyCatalog:
    policies: tuple[tuple[str, Policy], ...]
    tenants: tuple[tuple[str, str], ...]

    def __post_init__(self):
        for collection in (self.policies, self.tenants):
            if type(collection) is not tuple or any(type(pair) is not tuple or len(pair) != 2 for pair in collection):
                raise PolicyConfigurationError("Catalog must contain immutable pairs")
        if (not self.policies or any(not isinstance(p, Policy) or version != p.version for version, p in self.policies)
                or len(dict(self.policies)) != len(self.policies) or not self.tenants
                or len(dict(self.tenants)) != len(self.tenants)
                or any(not isinstance(tenant, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tenant) is None
                       or not isinstance(version, str)
                       or version not in dict(self.policies) for tenant, version in self.tenants)):
            raise PolicyConfigurationError("Invalid policy catalog")

    def for_tenant(self, tenant):
        # Unknown tenants never inherit another institution's policy.
        version = dict(self.tenants)[tenant]
        return dict(self.policies)[version]

    @classmethod
    def from_mapping(cls, value):
        try:
            Draft202012Validator(configuration_schema()).validate(value)
            policies = []
            for version, settings in value["policies"].items():
                parameters = settings["parameters"]
                converted = {k: tuple(v.items()) if k == "family_caps"
                             else tuple(v) if isinstance(v, list) else v for k, v in parameters.items()}
                criteria = tuple((code, tuple(args.items())) for code, args in settings["criteria"].items())
                policies.append((version, Policy(version=version, rule_parameters=criteria, **converted)))
            if any(version not in dict(policies) for version in value["tenants"].values()):
                raise PolicyConfigurationError("Tenant references an unknown policy version")
            return cls(tuple(policies), tuple(value["tenants"].items()))
        except PolicyConfigurationError:
            raise
        except (ValueError, TypeError, KeyError, JsonSchemaValidationError) as error:
            raise PolicyConfigurationError("Invalid policy configuration") from error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PolicyConfigurationError("Duplicate policy JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise PolicyConfigurationError("Non-finite policy JSON constant")


def load_policy_catalog(path=DEFAULT_POLICY_PATH):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=_invalid_constant)
        return PolicyCatalog.from_mapping(data)
    except PolicyConfigurationError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise PolicyConfigurationError("Cannot load policy configuration") from error
