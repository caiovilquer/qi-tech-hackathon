"""HTTP application service: durable claims, outside-lock I/O and final revalidation."""
import asyncio
from dataclasses import dataclass, replace
from hmac import compare_digest
import json
import re

from sqlalchemy.exc import DBAPIError

from antifraud.adapter import to_pix
from antifraud.contract import parse_payload_json, normalize_payload, ValidationError
from antifraud.engine import preliminary
from dtos.pix_storage import Busy, HashConflict, Replay, EventInput
from controllers.pix_storage import identity
from errors.pix import PixError
from providers.budget import Deadline, BudgetExceeded
from providers.contracts import ReputationKey, SecurityScope, ProviderDataError
from providers.enrichment import Enricher, EnrichmentNeedsRefresh, SourceCheck, utc_now
from providers.selection import select_security, select_reputation, reconcile_payments
from utils.logger import get_logger
from utils.pix_deadline import in_database
from utils.pix_storage_codec import fingerprint

logger = get_logger(__name__)


@dataclass(frozen=True)
class Tenant:
    name: str
    token: str
    participant: str

    def __post_init__(self):
        if (not isinstance(self.name,str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}',self.name) is None
                or not isinstance(self.token,str) or not self.token
                or not isinstance(self.participant,str) or re.fullmatch(r'[0-9]{8}',self.participant) is None):
            raise ValueError('Invalid Pix tenant credential/ISPB configuration')


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:
            raise ValueError('Duplicate server configuration key')
        result[key]=value
    return result


def load_tenants(raw, internal_token, catalog):
    data = json.loads(raw,object_pairs_hook=unique_object) if raw else {'demo':{'token':internal_token,'participant':'00000000'}}
    if not isinstance(data,dict):
        raise ValueError('Pix tenants must be an object')
    tenants = tuple(Tenant(name, **settings) for name,settings in data.items())
    if not tenants or len({t.token for t in tenants})!=len(tenants):
        raise ValueError('Pix credentials must be nonempty and unambiguous')
    for t in tenants:
        catalog.for_tenant(t.name)
    return tenants


def resolve_tenant(tenants, credential):
    if not isinstance(credential,str):
        return None
    # Encode so non-ASCII credentials never trigger compare_digest TypeError.
    return next((t for t in tenants if compare_digest(t.token.encode(), credential.encode())), None)


class Refresh(RuntimeError):
    """Release locks and obtain/validate external material in another cycle."""


class PixController:
    def __init__(self, storage, bank, reputation):
        self.storage, self.bank, self.reputation = storage, bank, reputation

    async def receive(self, request_id, tenant, deadline):
        return await in_database(deadline,self.storage.receive,request_id,tenant)

    async def finish(self, receipt, tenant, status, category, deadline, *, key=None, strict=False):
        if receipt is None:
            return
        try:
            await in_database(deadline,self.storage.finish_receipt,receipt,tenant,status,category,analysis_key=key)
        except Exception:
            # No claim that receipt outcome is durable when PostgreSQL failed.
            logger.error('Pix receipt outcome could not be persisted')
            if strict:
                raise

    async def get(self, tenant, external_id, deadline):
        try:
            identity(tenant,external_id)
        except ValueError:
            raise PixError(404,'QIT002001')
        try:
            result = await in_database(deadline,self.storage.get,tenant,external_id)
        except Exception as error:
            logger.error('Pix GET storage failure')
            raise PixError() from error
        if result is None:
            raise PixError(404,'QIT002001')
        return result.public

    async def wait_claim(self, operation, deadline, *args):
        while True:
            try:
                return await in_database(deadline,operation,*args,
                    lock_timeout_ms=max(1,min(1000,int(deadline.remaining()*1000))))
            except Busy:
                await asyncio.sleep(min(.025,deadline.remaining()))

    def _base(self, pix, policy):
        with self.storage.read() as r:
            now=utc_now()
            keys=tuple(ReputationKey(pix.payer.tenant,doc,self.reputation.identity,'person_reputation','pix')
                       for doc in (pix.payer.document,pix.payee_document))
            cached=tuple(record for key in keys if (record:=r.reputation(key,now)) is not None)
            return (r.context(pix,now,policy),cached,
                    r.security(SecurityScope.from_pix(pix),self.bank.identity,now),r.bank_links(pix.payer))

    def _persist(self, enrichment):
        with self.storage.write() as r:
            r.put_snapshots(enrichment.cache_updates+(enrichment.security,))
        # Executions reconciled to local analyses must become durable local facts
        # before entering the final account transaction. Never infer a bank link.
        with self.storage.read() as r:
            base=r.context(enrichment.context.pix,utc_now(),enrichment.progress.policy)
            locals_={h.pix.id:h for h in base.history if h.pix.payer==base.pix.payer}
            missing=set(enrichment.context.execution_events)-set(base.execution_events)
            events=[]
            for e in missing:
                if e.available_at>base.decision_at or e.occurred_at>base.decision_at:
                    continue  # A source fact not known yet must not gain a local receipt now.
                original=locals_.get(e.pix_id)
                if original is None:
                    raise ProviderDataError('Linked execution has no local analysis')
                row=r.local(base.pix.payer.tenant,e.pix_id)
                provider,event_id=e.event_id.split(':',1)
                events.append(EventInput(base.pix.payer.tenant,provider,event_id,'execution',e.payer,e.pix_id,
                    str(row['key']),fingerprint(original.pix),e.state,e.occurred_at))
        for value in events:
            self.storage.ingest(value)

    def _finalize(self, ticket, normalized, enriched, policy, receipt, tenant):
        pix=to_pix(normalized,tenant.name)
        with self.storage.analysis(ticket,pix) as work:
            now=work.now(); r=work.repository
            local=work.context(pix,now,policy)
            known_rows=tuple(row for row in enriched.progress.bank_rows
                             if row.execution.available_at<=now and row.execution.occurred_at<=now)
            reconciled=reconcile_payments(local,known_rows,r.bank_links(pix.payer))
            if set(reconciled.execution_events)-set(local.execution_events):
                raise Refresh('A new bank link requires ingestion outside final locks')
            local=reconciled
            # Retain verified imported executions while replacing every local fact
            # with the locked read. No provider I/O is performed in this block.
            security=select_security(SecurityScope.from_pix(pix),self.bank.identity,(enriched.security,),now,
                cached=r.security(SecurityScope.from_pix(pix),self.bank.identity,now))
            if security != enriched.security:
                raise Refresh('Bank context changed during enrichment')
            if security.state=='stale':
                raise Refresh('Bank context expired under locks')
            evidence=[]; actions=dict(enriched.context.dict_actions)
            for role,doc in (('payer',pix.payer.document),('payee',pix.payee_document)):
                key=ReputationKey(tenant.name,doc,self.reputation.identity,'person_reputation','pix')
                rows=tuple(x for x in enriched.progress.selected if x.key==key)
                stored=r.reputation(key,now)
                selected=select_reputation(key,rows,now,cached=stored) if rows or stored else None
                previous=select_reputation(key,rows,now) if rows else None
                if stored is not None and selected!=previous:
                    raise Refresh('Reputation changed during enrichment')
                if actions.get(role)=='cache' and (selected is None or not selected.evidence.valid(now)):
                    raise Refresh('Cached reputation expired under locks')
                if selected is not None and (role in enriched.context.dict_queried or selected.evidence.valid(now)):
                    evidence.append((role,selected.evidence))
                    if role not in enriched.context.dict_queried:
                        actions[role]='cache'
            context=replace(local,security=security.snapshot,bank_payments=local.bank_payments,
                dict_evidence=tuple(evidence),dict_queried=enriched.context.dict_queried,
                dict_actions=tuple(actions.items()),provider_traces=enriched.context.provider_traces)
            if actions.get('payee')=='skipped_confidence' and preliminary(context,policy).c_pre<policy.dict_threshold:
                raise Refresh('Locked context now requires recipient lookup')
            return work.complete(normalized,context,policy,receipt_id=receipt,source_participant=tenant.participant)

    async def post(self, raw, tenant, policy, receipt, total):
        # Reserve 100 ms of the same 3 s total for finalizing an error receipt.
        deadline=Deadline(total.expires_at-.1,total.clock)
        try:
            parsed=parse_payload_json(raw)
            try:
                normalized=normalize_payload(parsed)
            except ValidationError:
                # An invalid Pix is never a claim/analysis. Preserve a readable ID
                # when it can be extracted unambiguously, without inventing a hash.
                candidate=parsed.get('id') if isinstance(parsed,dict) else None
                if isinstance(candidate,str) and candidate.isprintable() and 1<=len(candidate)<=128:
                    await in_database(deadline,self.storage.identify_receipt,receipt,tenant.name,candidate,None)
                raise
            pix=to_pix(normalized,tenant.name)
            await in_database(deadline,self.storage.identify_receipt,receipt,tenant.name,pix.id,normalized.payload_hash)
            ticket=await self.wait_claim(self.storage.claim,deadline,tenant.name,pix.id,normalized.payload_hash)
            if isinstance(ticket,Replay):
                await self.finish(receipt,tenant.name,200,'replay',total,key=ticket.dto.public['key'],strict=True)
                return 200,ticket.dto.public
            initial=await self.wait_claim(self.storage.claim_initial,deadline,ticket,pix.payer)
            progress=None
            async def complete_initial(result):
                nonlocal initial
                if initial is not None:
                    await in_database(deadline,self.storage.complete_initial,initial,result)
                    initial=None
            for cycle in range(3):
                base,cached,security,links=await in_database(deadline,self._base,pix,policy)
                try:
                    enriched=await Enricher(self.bank,self.reputation,policy).enrich(base,
                        source_check=SourceCheck.OWNED if initial else SourceCheck.COMPLETED,
                        deadline=deadline,cached=cached,security_cached=security,links=links,resume=progress,
                        on_initial=complete_initial)
                except EnrichmentNeedsRefresh as error:
                    progress=error.partial
                    if progress.initial_result is not None:
                        await complete_initial(progress.initial_result)
                    continue
                progress=enriched.progress
                await in_database(deadline,self._persist,enriched)
                try:
                    dto=await in_database(deadline,self._finalize,ticket,normalized,enriched,policy,receipt,tenant)
                    return 201,dto.public
                except Refresh:
                    continue
            raise BudgetExceeded('Context did not stabilize in three cycles')
        except ValidationError as error:
            outcome=PixError(error.http_status,error.code)
        except HashConflict:
            outcome=PixError(409,'QIT002002')
        except DBAPIError as error:
            # Only the named tenant/ID uniqueness constraint can be a race winner.
            if (getattr(error.orig,'pgcode',None)=='23505' and
                    getattr(getattr(error.orig,'diag',None),'constraint_name',None)=='uq_pix_tenant_external_id'):
                try:
                    winner=await in_database(deadline,self.storage.claim,tenant.name,pix.id,normalized.payload_hash)
                    if isinstance(winner,Replay):
                        await self.finish(receipt,tenant.name,200,'replay',total,key=winner.dto.public['key'],strict=True)
                        return 200,winner.dto.public
                except HashConflict:
                    await self.finish(receipt,tenant.name,409,'QIT002002',total)
                    raise PixError(409,'QIT002002')
                except Exception:
                    logger.error('Pix race winner could not be read')
            outcome=PixError()
        except Exception:
            logger.error('Pix analysis could not be committed reliably')
            outcome=PixError()
        await self.finish(receipt,tenant.name,outcome.http_status,outcome.code,total)
        raise outcome
