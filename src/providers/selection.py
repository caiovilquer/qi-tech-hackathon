"""Pure version selection and exact bank-identity reconciliation; no cache/storage I/O."""
from dataclasses import asdict, replace
from datetime import timedelta, timezone

from antifraud.context import BankPayment, aware
from .contracts import (BankRecord, ReputationRecord, ProviderDataError, account, detect_ownership)
from antifraud.context import DictEvidence


def _latest(rows, value, as_of):
    aware(as_of)
    known = [r for r in rows if value(r).observed_at <= as_of and value(r).available_at <= as_of]
    versions = {}
    for row in known:
        item = value(row)
        identity = (item.observed_at.astimezone(timezone.utc), item.source_version)
        if identity in versions:
            first = value(versions[identity])
            a, b = asdict(first), asdict(item)
            a.pop('available_at'); b.pop('available_at')
            # Staleness is coverage metadata, not a changed source fact.
            a.pop('state', None); b.pop('state', None)
            if a != b:
                raise ProviderDataError('Conflicting content for same source version')
            if getattr(versions[identity], 'provenance', None) != getattr(row, 'provenance', None):
                raise ProviderDataError('Conflicting provenance for same source version')
            if isinstance(row, ReputationRecord) and (first.state == 'stale' or item.state == 'stale'):
                row = replace(row, evidence=replace(item, state='stale', available_at=min(first.available_at, item.available_at)))
            elif first.available_at <= item.available_at:
                continue
        versions[identity] = row
    return versions[max(versions)] if versions else None


def select_security(scope, provider, rows, as_of, cached=None):
    aware(as_of)
    as_of = as_of.astimezone(timezone.utc)
    items = tuple(rows) + ((cached,) if cached is not None else ())
    if any(not isinstance(r, BankRecord) or r.scope != scope or r.provider != provider for r in items):
        raise ProviderDataError('Wrong security scope/provider')
    facts = [r for r in items if r.snapshot is not None]
    latest = _latest(facts, lambda r: r.snapshot, as_of)
    if latest is not None:
        if latest.state == 'stale' and latest.snapshot.valid(as_of):
            raise ProviderDataError('Stale bank coverage contradicts a current authorization snapshot')
        return replace(latest, state='available' if latest.snapshot.valid(as_of) else 'stale')
    state = 'unavailable' if any(r.state == 'unavailable' for r in items) else 'not_covered'
    return BankRecord(scope, provider, state, None, provider.name)


def select_reputation(key, rows, as_of, cached=None, ttl_seconds=300):
    aware(as_of)
    as_of = as_of.astimezone(timezone.utc)
    items = tuple(rows) + ((cached,) if cached is not None else ())
    if any(not isinstance(r, ReputationRecord) or r.key != key for r in items):
        raise ProviderDataError('Wrong reputation scope/provider')
    # Unknown coverage is not a clean source revision and cannot replace known facts.
    facts = [r for r in items if r.evidence.state in ('available', 'stale')]
    latest = _latest(facts, lambda r: r.evidence, as_of)
    if latest is not None:
        item = latest.evidence
        state = 'available' if item.state == 'available' and item.valid(as_of) else 'stale'
        return replace(latest, evidence=replace(item, state=state))
    state = 'unavailable' if any(r.evidence.state == 'unavailable' for r in items) else 'not_covered'
    return ReputationRecord(key, DictEvidence(key.document, state, as_of, as_of,
        as_of+timedelta(seconds=ttl_seconds), provenance=key.provider.name))


def _payment_fingerprint(pix):
    # Device/channel are not financial identity; amount/time/owners/accounts are.
    return (pix.payer, pix.payee_document, pix.destination, pix.amount_cents,
            pix.transaction_at.astimezone(timezone.utc), pix.destination_opened_at.astimezone(timezone.utc))


def reconcile_payments(context, rows, links):
    payer = context.pix.payer
    account(payer)
    account_id = (payer.tenant, payer.branch, payer.account)
    detect_ownership((payer,) + tuple(h.pix.payer for h in context.history
        if (h.pix.payer.tenant, h.pix.payer.branch, h.pix.payer.account) == account_id))
    identities, execution_ids = {}, {}
    for row in tuple(context.bank_payments) + tuple(rows):
        if not isinstance(row, BankPayment) or row.pix.payer != payer:
            raise ProviderDataError('Bank history outside requested account/tenant')
        execution_key = (payer.tenant, row.provider, row.execution.event_id)
        if execution_key in execution_ids and execution_ids[execution_key] != row.operation_id:
            raise ProviderDataError('Execution identifier reused by different bank operations')
        execution_ids[execution_key] = row.operation_id
        key = (payer.tenant, row.provider, row.operation_id)
        if key in identities and identities[key] != row:
            raise ProviderDataError('Conflicting bank operation identity')
        identities[key] = row
    mapping, targets = {}, {}
    for link in links:
        if link.tenant != payer.tenant:
            raise ProviderDataError('Bank link outside tenant')
        key = (link.tenant, link.provider, link.operation_id)
        if key in mapping and mapping[key] != link.local_pix_id:
            raise ProviderDataError('Conflicting bank link')
        target = (link.tenant, link.provider, link.local_pix_id)
        if target in targets and targets[target] != key:
            raise ProviderDataError('Multiple bank operations linked to one local operation')
        targets[target] = key
        mapping[key] = link.local_pix_id
    local = {h.pix.id: h for h in context.history if h.pix.payer == payer}
    local[context.pix.id] = context.pix
    events, imported = list(context.execution_events), []
    for key, row in identities.items():
        target = mapping.get(key)
        if target is None:
            if row.pix.id in local:
                raise ProviderDataError('Local overlap requires an explicit bank identity link')
            imported.append(row)
            continue
        if target not in local:
            raise ProviderDataError('Bank link references unknown local operation')
        original = local[target]
        pix = original.pix if hasattr(original, 'pix') else original
        if _payment_fingerprint(pix) != _payment_fingerprint(row.pix):
            raise ProviderDataError('Bank link conflicts with local financial identity')
        if target == context.pix.id:
            continue  # Never create a prior payment from the operation under analysis.
        event = replace(row.execution, pix_id=target, event_id=f'{row.provider}:{row.execution.event_id}')
        previous = [e for e in events if e.payer == payer and e.pix_id == target]
        if any(e.state != 'executed' or e.occurred_at != event.occurred_at for e in previous):
            raise ProviderDataError('Bank payment conflicts with terminal local execution')
        # Same provider/execution ID is one fact; local receipt time is knowledge metadata.
        if not any(e.event_id == event.event_id for e in previous):
            events.append(event)
    return replace(context, bank_payments=tuple(imported), execution_events=tuple(events))
