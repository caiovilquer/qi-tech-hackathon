"""Versioned JSON codec with an explicit domain whitelist; never pickle/eval."""
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json

from antifraud import context as domain
from providers import contracts

TYPES={c.__name__:c for c in (domain.AccountKey,domain.DestinationKey,domain.Pix,domain.Restriction,
    domain.SecuritySnapshot,domain.DictEvidence,domain.ExecutionEvent,domain.AuthEvent,domain.BankPayment,
    contracts.ProviderIdentity,contracts.SecurityScope,contracts.ReputationKey,contracts.ReputationRecord,contracts.BankRecord)}


def _encode(value):
    if isinstance(value,datetime):
        domain.aware(value)
        return {'datetime':value.astimezone(timezone.utc).isoformat()}
    if type(value) is tuple:
        return {'tuple':[_encode(v) for v in value]}
    if is_dataclass(value) and type(value) in TYPES.values():
        return {'type':type(value).__name__,'data':{f.name:_encode(getattr(value,f.name)) for f in fields(value)}}
    if value is None or type(value) in (str,int,bool):
        return value
    raise ValueError('Unsupported storage type')


def _decode(value):
    if type(value) is dict:
        if set(value)=={'datetime'}:
            try:
                result=datetime.fromisoformat(value['datetime'])
            except (TypeError,ValueError) as error:
                raise ValueError('Invalid storage datetime') from error
            domain.aware(result)
            return result
        if set(value)=={'tuple'} and type(value['tuple']) is list:
            return tuple(_decode(v) for v in value['tuple'])
        if set(value)=={'type','data'} and value['type'] in TYPES:
            kind=TYPES[value['type']]
            if type(value['data']) is not dict or set(value['data'])!={f.name for f in fields(kind)}:
                raise ValueError('Invalid storage fields')
            try:
                # Whitelisted constructors validate their own domain invariants.
                return kind(**{k:_decode(v) for k,v in value['data'].items()})
            except (TypeError,AttributeError,OverflowError) as error:
                raise ValueError(f'Invalid storage content for {kind.__name__}') from error
        raise ValueError('Unknown storage type/tag')
    if value is None or type(value) in (str,int,bool):
        return value
    raise ValueError('Invalid storage scalar')


def pack(value):
    return {'version':1,'value':_encode(value)}


def unpack(value):
    if type(value) is not dict or set(value)!={'version','value'} or type(value['version']) is not int or value['version']!=1:
        raise ValueError('Unsupported storage version')
    return _decode(value['value'])


def digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False).encode()).hexdigest()


def fingerprint(pix):
    # Local external ID/key are separately bound; no matching by amount/time.
    return digest(pack((pix.payer,pix.payee_document,pix.destination,pix.amount_cents,
                        pix.transaction_at,pix.destination_opened_at)))
