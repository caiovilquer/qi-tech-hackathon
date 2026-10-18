"""Tenant credentials and server-side fixture loading fail closed at startup."""
import json
import pytest
from antifraud.configuration import load_policy_catalog
from utils.pix_tenants import load_tenants, resolve_tenant
from providers.configuration import load_fixture_providers
from providers.contracts import ProviderDataError, ProviderIdentity
from tests.unit.test_pix_provider_selection import security_record, reputation_record
from utils.pix_storage_codec import pack


@pytest.mark.parametrize('raw',[
    '{}','[]','null',
    '{"demo":{"token":"x","participant":"bad"}}',
    '{"unknown":{"token":"x","participant":"00000000"}}',
    '{"demo":{"token":"x","participant":"00000000"},"demo":{"token":"y","participant":"00000000"}}',
])
def test_invalid_tenant_configuration_fails_closed(raw):
    with pytest.raises((ValueError,TypeError,KeyError)):
        load_tenants(raw,'test_token',load_policy_catalog())


def test_default_credential_is_bound_only_to_configured_demo():
    tenants=load_tenants(None,'test_token',load_policy_catalog())
    assert resolve_tenant(tenants,'test_token').name=='demo'
    assert resolve_tenant(tenants,'') is None
    assert resolve_tenant(tenants,'é') is None
    assert resolve_tenant(tenants,None) is None


def test_fixture_dataset_is_whitelisted_and_uses_same_provider_contracts(tmp_path):
    from dataclasses import replace
    from providers.contracts import ReputationKey
    bank=replace(security_record(),provider=ProviderIdentity('fixture-bank','v1'))
    reputation=reputation_record()
    reputation=replace(reputation,key=replace(reputation.key,provider=ProviderIdentity('fixture-dict','v1')))
    path=tmp_path/'fixtures.json'
    path.write_text(json.dumps({'bank':[pack(bank)],'reputation':[pack(reputation)]}))
    b,r=load_fixture_providers(path)
    assert b.records==(bank,) and r.records==(reputation,)
    path.write_text('{"bank":[{"$type":"os.system","value":"command"}]}')
    with pytest.raises((ValueError,ProviderDataError)):load_fixture_providers(path)
