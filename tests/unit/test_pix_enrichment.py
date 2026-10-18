"""Enrichment integration contracts; no PostgreSQL/idempotency claims."""
from dataclasses import replace
from datetime import timedelta
import unittest

from antifraud.context import Context, Restriction, REJECTED, TWO_FA
from antifraud.engine import evaluate
from antifraud.policy import Policy
from providers.budget import Deadline
from providers.contracts import ProviderDataError, SecurityScope
from providers.enrichment import Enricher, SourceCheck
from providers.fixtures import FixtureBankProvider, FixtureReputationProvider
from tests.unit.test_pix_provider_selection import IDENTITY, BASE, A, B, example_pix, security_record, reputation_record
from tests.support.pix_scenarios import Simulator, seed_profile


class RecordingBank:
    def __init__(self, delegate):
        self.delegate, self.identity, self.calls = delegate, delegate.identity, []
    async def security(self, scope, as_of, *, timeout_seconds):
        self.calls.append(('security', scope))
        return await self.delegate.security(scope, as_of, timeout_seconds=timeout_seconds)
    async def payments(self, payer, as_of, *, timeout_seconds):
        self.calls.append(('payments', payer))
        return await self.delegate.payments(payer, as_of, timeout_seconds=timeout_seconds)


class RecordingReputation:
    def __init__(self, delegate):
        self.delegate, self.identity, self.calls = delegate, delegate.identity, []
    async def reputation(self, key, as_of, *, timeout_seconds):
        self.calls.append(key)
        return await self.delegate.reputation(key, as_of, timeout_seconds=timeout_seconds)


def profiled(p=None):
    sim = Simulator()
    seed_profile(sim)
    return Context(p or example_pix('current'), BASE, tuple(sim.history), tuple(sim.executions))


class EnrichmentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.p = example_pix('current')
        self.bank = RecordingBank(FixtureBankProvider(IDENTITY))
        self.reputation = RecordingReputation(FixtureReputationProvider(IDENTITY))

    async def enrich(self, c=None, source=SourceCheck.OWNED, **kwargs):
        return await Enricher(self.bank, self.reputation, Policy()).enrich(
            c or Context(self.p, BASE), source_check=source, deadline=Deadline.after(3), **kwargs)

    async def test_first_request_missing_is_true_unknown_and_penalty_once(self):
        result = await self.enrich()
        self.assertEqual(result.initial_result.evidence.state, 'not_covered')
        self.assertEqual(dict(result.context.dict_actions), {'payer':'queried', 'payee':'queried'})
        self.assertEqual(evaluate(result.context).score, 10)
        self.assertEqual(len(self.reputation.calls), 2)
        self.assertIsNone(result.context.security)
        self.assertEqual(result.security.state, 'not_covered')
        self.assertEqual(result.history_state, 'not_covered')

    async def test_threshold_eighty_skips_recipient_and_completed_source(self):
        result = await self.enrich(profiled(), SourceCheck.COMPLETED)
        self.assertEqual(evaluate(result.context).c_pre, 80)
        self.assertEqual(dict(result.context.dict_actions), {'payer':'skipped_after_initial','payee':'skipped_confidence'})
        self.assertEqual(self.reputation.calls, [])
        self.assertEqual([kind for kind,_ in self.bank.calls], ['security','payments'])

    async def test_current_bank_security_is_queried_even_with_confidence_one_hundred(self):
        self.bank = RecordingBank(FixtureBankProvider(IDENTITY, (security_record(),)))
        result = await self.enrich(profiled(), SourceCheck.COMPLETED)
        self.assertEqual(evaluate(result.context).c_pre, 100)
        self.assertEqual(len(self.bank.calls), 2)
        self.assertEqual(result.context.security.device_state, 'registered')
        self.assertEqual(self.reputation.calls, [])

    async def test_known_adverse_or_restriction_applies_despite_high_confidence(self):
        self.bank = RecordingBank(FixtureBankProvider(IDENTITY, (security_record(),)))
        for restricted in (False, True):
            r = Restriction(B, 'payee', BASE-timedelta(minutes=1), BASE-timedelta(minutes=1), BASE+timedelta(minutes=5))
            cached = reputation_record(fraud_count=0 if restricted else 1, restrictions=(r,) if restricted else ())
            result = await self.enrich(profiled(), SourceCheck.COMPLETED, cached=(cached,))
            self.assertEqual(evaluate(result.context).c_pre, 100)
            self.assertEqual(dict(result.context.dict_actions)['payee'], 'cache')
            self.assertEqual(evaluate(result.context).status, REJECTED if restricted else TWO_FA)
        self.assertEqual(self.reputation.calls, [])

    async def test_initial_check_is_not_restarted_by_rejection_outage_or_device_change(self):
        self.bank = RecordingBank(FixtureBankProvider(IDENTITY, (security_record(device_state='revoked'),)))
        payer_key = reputation_record(document=A).key
        self.reputation = RecordingReputation(FixtureReputationProvider(IDENTITY, unavailable=(payer_key,)))
        first = await self.enrich()
        self.assertEqual(evaluate(first.context).status, REJECTED)
        self.assertEqual(first.initial_result.evidence.state, 'unavailable')
        payer_attempts = len([key for key in self.reputation.calls if key.document==A])
        second = await self.enrich(Context(replace(self.p, id='next', device_id='other'), BASE), SourceCheck.COMPLETED)
        self.assertIsNone(second.initial_result)
        self.assertEqual(len([key for key in self.reputation.calls if key.document==A]), payer_attempts)
        self.assertEqual(dict(second.context.dict_actions)['payer'], 'skipped_after_initial')

    async def test_same_document_reuses_query_and_applies_no_duplicate_signal(self):
        p = example_pix('current', payee=A)
        self.reputation = RecordingReputation(FixtureReputationProvider(IDENTITY, (reputation_record(A, fraud_count=1),)))
        result = await self.enrich(Context(p, BASE))
        self.assertEqual(len(self.reputation.calls), 1)
        self.assertEqual(evaluate(result.context).score, 30)
        self.assertEqual(dict(result.context.dict_actions), {'payer':'queried', 'payee':'cache'})

    async def test_same_document_outage_is_coalesced_but_both_roles_remain_unknown(self):
        p = example_pix('current', payee=A)
        self.reputation = RecordingReputation(FixtureReputationProvider(IDENTITY, unavailable=(reputation_record(A).key,)))
        result = await self.enrich(Context(p, BASE))
        self.assertEqual(len(self.reputation.calls), 2)  # one logical query + one technical retry
        self.assertEqual(result.context.dict_queried, ('payer','payee'))
        self.assertEqual(evaluate(result.context).score, 10)
        self.assertEqual(len(result.lookups), 1)

    async def test_expired_watermark_blocks_older_clean_reply(self):
        old = reputation_record()
        newest = reputation_record(observed_at=BASE-timedelta(minutes=1), available_at=BASE-timedelta(minutes=1),
                                   valid_until=BASE, fraud_count=1)
        self.reputation = RecordingReputation(FixtureReputationProvider(IDENTITY, (old,)))
        result = await self.enrich(cached=(newest,))
        used = dict(result.context.dict_evidence)['payee']
        self.assertEqual(used.state, 'stale')
        self.assertEqual(used.fraud_count, 1)
        self.assertIn('DICT_CHECK_UNAVAILABLE', {r.code for r in evaluate(result.context).reasons})
        self.assertEqual(next(r for r in result.cache_updates if r.key.document==B).evidence.observed_at, newest.evidence.observed_at)

    async def test_foreign_tenant_cache_does_not_skip_queries(self):
        result = await self.enrich(cached=(reputation_record(tenant='other'),))
        self.assertEqual(len(self.reputation.calls), 2)
        self.assertEqual(result.context.dict_queried, ('payer','payee'))

    async def test_provider_subject_or_security_scope_mismatch_is_technical_error(self):
        class WrongReputation(RecordingReputation):
            async def reputation(self, key, as_of, *, timeout_seconds):
                return reputation_record(A, tenant='other')
        self.reputation = WrongReputation(FixtureReputationProvider(IDENTITY))
        with self.assertRaises(ProviderDataError):
            await self.enrich()
        self.reputation = RecordingReputation(FixtureReputationProvider(IDENTITY))
        class WrongBank(RecordingBank):
            async def security(self, scope, as_of, *, timeout_seconds):
                return security_record(replace(self_p, device_id='wrong'))
        self_p = self.p
        self.bank = WrongBank(FixtureBankProvider(IDENTITY))
        with self.assertRaises(ProviderDataError):
            await self.enrich()

    async def test_unowned_or_in_progress_initial_check_cannot_be_skipped(self):
        with self.assertRaises(ValueError):
            await self.enrich(source='in_progress')
        self.assertEqual(self.bank.calls, [])
        self.assertEqual(self.reputation.calls, [])

    async def test_bank_history_is_integrated_without_fake_receipts(self):
        from tests.unit.test_pix_provider_selection import payment
        rows = tuple(payment(str(i), 3+i) for i in range(5))
        self.bank = RecordingBank(FixtureBankProvider(IDENTITY, (security_record(),), rows))
        result = await self.enrich(source=SourceCheck.COMPLETED)
        self.assertTrue(evaluate(result.context).general_profile)
        self.assertEqual(result.context.history, ())
        self.assertEqual(len(result.context.bank_payments), 5)
        self.assertEqual(evaluate(result.context).c_pre, 100)

    async def test_provider_provenance_and_coverage_reach_private_dto_audit(self):
        from copy import deepcopy
        from antifraud.adapter import to_pix
        from antifraud.contract import normalize_payload
        from antifraud.dto import PixTransactionDTO
        from tests.unit.test_pix_contract import OFFICIAL_PAYLOAD
        normalized = normalize_payload(deepcopy(OFFICIAL_PAYLOAD))
        p = to_pix(normalized, 'demo')
        result = await self.enrich(Context(p, BASE))
        dto = PixTransactionDTO.from_decision('58ce6c65-0d45-4d5f-b5c8-2e65585a3fb8', normalized,
                                             result.context, Policy(), evaluate(result.context))
        traces = dto.audit['provider_traces']
        self.assertEqual({r['role'] for r in traces}, {'security','history','payer','payee'})
        self.assertTrue(all(r['provider']=='bank-demo' and r['version']=='v1' for r in traces))
        self.assertEqual(next(r for r in traces if r['role']=='security')['state'], 'not_covered')
        self.assertNotIn('provider_traces', dto.public)

    async def test_total_deadline_exhaustion_propagates_without_fake_unknown_result(self):
        from providers.budget import BudgetExceeded
        now = [0.0]
        class LateBank(RecordingBank):
            async def security(self, scope, as_of, *, timeout_seconds):
                now[0] = 4
                return security_record()
        self.bank = LateBank(FixtureBankProvider(IDENTITY))
        with self.assertRaises(BudgetExceeded):
            await Enricher(self.bank, self.reputation, Policy()).enrich(
                Context(self.p, BASE), source_check=SourceCheck.OWNED, deadline=Deadline(3, lambda: now[0]))
        self.assertEqual(self.reputation.calls, [])

    async def test_bank_outage_remains_distinct_in_private_trace(self):
        self.bank = RecordingBank(FixtureBankProvider(IDENTITY, unavailable=(SecurityScope.from_pix(self.p),)))
        result = await self.enrich()
        self.assertEqual(result.security.state, 'unavailable')
        self.assertEqual(result.history_state, 'unavailable')
        self.assertIsNone(result.context.security)
        traces = {trace.role: trace for trace in result.context.provider_traces}
        self.assertEqual(traces['security'].lookup_state, 'unavailable')
        self.assertEqual(traces['history'].state, 'unavailable')
