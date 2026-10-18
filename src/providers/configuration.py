"""Load only explicit normalized fixture datasets; never supplied by HTTP clients."""
import json
from pathlib import Path
from providers.contracts import ProviderIdentity
from providers.fixtures import FixtureBankProvider, FixtureReputationProvider
from utils.pix_storage_codec import unpack


def load_fixture_providers(path=None):
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:
                raise ValueError('Duplicate fixture configuration key')
            value[key]=item
        return value
    data=json.loads(Path(path).read_text(),object_pairs_hook=unique) if path else {}
    if not isinstance(data,dict) or set(data)-{'bank','history','reputation'}:
        raise ValueError('Unknown fixture dataset fields')
    if any(not isinstance(value,list) for value in data.values()):
        raise ValueError('Fixture datasets must be arrays')
    records={key:tuple(unpack(row) for row in data.get(key,[])) for key in ('bank','history','reputation')}
    return (FixtureBankProvider(ProviderIdentity('fixture-bank','v1'),records['bank'],records['history']),
            FixtureReputationProvider(ProviderIdentity('fixture-dict','v1'),records['reputation']))
