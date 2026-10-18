"""Pix tenants: institution name, credential and ISPB configured on the server, never sent by clients."""
from dataclasses import dataclass
from hmac import compare_digest
import json
import re

TENANT_NAME = re.compile(r'[A-Za-z0-9_-]{1,64}')
ISPB = re.compile(r'[0-9]{8}')
DEFAULT_TENANT = 'demo'
DEFAULT_PARTICIPANT = '00000000'


@dataclass(frozen=True)
class Tenant:
    name: str
    token: str
    participant: str

    def __post_init__(self):
        if (not isinstance(self.name, str) or TENANT_NAME.fullmatch(self.name) is None
                or not isinstance(self.token, str) or not self.token
                or not isinstance(self.participant, str) or ISPB.fullmatch(self.participant) is None):
            raise ValueError('Invalid Pix tenant credential/ISPB configuration')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate server configuration key')
        result[key] = value
    return result


def load_tenants(raw, internal_token, catalog):
    """Parses PIX_TENANTS (JSON) or defaults to `demo` with INTERNAL_TOKEN; every tenant needs a policy."""
    data = json.loads(raw, object_pairs_hook=unique_object) if raw else {
        DEFAULT_TENANT: {'token': internal_token, 'participant': DEFAULT_PARTICIPANT}}
    if not isinstance(data, dict):
        raise ValueError('Pix tenants must be an object')
    tenants = tuple(Tenant(name, **settings) for name, settings in data.items())
    if not tenants or len({tenant.token for tenant in tenants}) != len(tenants):
        raise ValueError('Pix credentials must be nonempty and unambiguous')
    for tenant in tenants:
        catalog.for_tenant(tenant.name)
    return tenants


def resolve_tenant(tenants, credential):
    if not isinstance(credential, str):
        return None
    # Byte comparison: a non-ASCII credential must not raise inside compare_digest.
    return next((tenant for tenant in tenants if compare_digest(tenant.token.encode(), credential.encode())), None)
