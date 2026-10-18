"""Regressions for the five independent PR 3 findings."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from antifraud.context import Context, DictEvidence, SecuritySnapshot
from antifraud.contract import normalize_payload, ValidationError
from antifraud.engine import evaluate
from antifraud.policy import Policy
from providers.budget import Deadline, ProviderRunner
from providers.contracts import BankRecord, ReputationRecord, ProviderDataError
from providers.enrichment import Enricher, SourceCheck
from providers.fixtures import FixtureBankProvider, FixtureReputationProvider
from tests.unit.test_pix_contract import OFFICIAL_PAYLOAD
from tests.unit.test_pix_provider_selection import (
    IDENTITY, BASE, A, example_pix, reputation_record, security_record, payment, history)


class LiveProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_responses_after_request_are_used(self):
        identity = IDENTITY
        class Bank:
            identity = IDENTITY
            async def security(self, scope, as_of, *, timeout_seconds):
                await asyncio.sleep(.001)
                t = datetime.now(timezone.utc)
                return BankRecord(scope, identity, 'available',
                    SecuritySnapshot(t, t, t+timedelta(minutes=5), 'revoked'), 'live_bank')
            async def payments(self, payer, as_of, *, timeout_seconds):
                return ()
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                await asyncio.sleep(.001)
                t = datetime.now(timezone.utc)
                return ReputationRecord(key, DictEvidence(key.document, 'available', t, t,
                    t+timedelta(minutes=5), fraud_count=3, provenance='live_dict'))
        start = datetime.now(timezone.utc)
        out = await Enricher(Bank(), Reputation(), Policy()).enrich(
            Context(replace(example_pix('current'), transaction_at=start), start),
            source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
        self.assertEqual(out.security.state, 'available')
        self.assertEqual(out.context.security.device_state, 'revoked')
        self.assertTrue(all(e.fraud_count == 3 for _, e in out.context.dict_evidence))
        self.assertGreater(out.context.decision_at, start)
        self.assertEqual(evaluate(out.context).status, 'rejected')
        self.assertIn('DEVICE_REVOKED', {r.code for r in evaluate(out.context).reasons})

    async def test_initial_result_is_selected_watermark_not_old_clean_reply(self):
        old = reputation_record(A)
        adverse = reputation_record(A, observed_at=BASE-timedelta(minutes=1),
            available_at=BASE-timedelta(minutes=1), valid_until=BASE, fraud_count=2)
        out = await Enricher(FixtureBankProvider(IDENTITY),
            FixtureReputationProvider(IDENTITY, (old,)), Policy(), clock=lambda: BASE).enrich(
                Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED,
                deadline=Deadline.after(3), cached=(adverse,))
        used = dict(out.context.dict_evidence)['payer']
        self.assertEqual(out.initial_result.evidence, used)
        self.assertEqual(used.state, 'stale')
        self.assertEqual(used.fraud_count, 2)
        self.assertEqual(out.lookups[0], old)  # raw is independently available for audit

    async def test_connection_reset_retries_and_becomes_unavailable(self):
        calls = []
        class BrokenBank:
            identity = IDENTITY
            async def security(self, scope, as_of, *, timeout_seconds):
                calls.append('security')
                raise ConnectionResetError('synthetic reset')
            async def payments(self, payer, as_of, *, timeout_seconds):
                calls.append('payments')
                raise ConnectionRefusedError('synthetic refusal')
        out = await Enricher(BrokenBank(), FixtureReputationProvider(IDENTITY), Policy()).enrich(
            Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
        self.assertEqual(calls, ['security', 'security', 'payments', 'payments'])
        self.assertEqual(out.security.state, 'unavailable')
        self.assertEqual(out.history_state, 'unavailable')
        self.assertIsNone(out.context.security)

    async def test_transport_recovery_and_non_transport_error_contract(self):
        attempts = []
        async def recover(timeout):
            attempts.append(timeout)
            if len(attempts) == 1:
                raise ConnectionResetError('synthetic reset')
            return 'ok'
        self.assertEqual(await ProviderRunner().call(recover, Deadline.after(3)), 'ok')
        self.assertEqual(len(attempts), 2)
        for error in (ProviderDataError('invalid'), ValueError('bug'), RuntimeError('bug')):
            calls = []
            async def invalid(timeout):
                calls.append(timeout)
                raise error
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                await ProviderRunner().call(invalid, Deadline.after(3))
            self.assertEqual(len(calls), 1)


class BoundaryRegressionTests(unittest.TestCase):
    def test_blank_device_rejected_at_normalization_with_public_error_contract(self):
        for device in (' ', '   ', '\t\n', '\u2003'):
            payload = deepcopy(OFFICIAL_PAYLOAD)
            payload['source']['device_id'] = device
            with self.subTest(device=repr(device)), self.assertRaises(ValidationError) as caught:
                normalize_payload(payload)
            self.assertEqual((caught.exception.http_status, caught.exception.code), (400, 'QIT000001'))
            self.assertEqual(caught.exception.path, 'source.device_id')

    def test_nonblank_device_identity_is_preserved_without_trimming(self):
        payload = deepcopy(OFFICIAL_PAYLOAD)
        payload['source']['device_id'] = ' device 😀 '
        self.assertEqual(normalize_payload(payload).canonical_payload['source']['device_id'], ' device 😀 ')

    def test_pure_pix_boundary_also_rejects_blank_device(self):
        with self.assertRaises(ValueError):
            replace(example_pix('current'), device_id='   ')

    def test_context_rejects_duplicate_and_conflicting_bank_operation(self):
        row = payment()
        conflict = replace(row, pix=replace(row.pix, amount_cents=row.pix.amount_cents+1))
        for second in (row, conflict):
            with self.subTest(second=second), self.assertRaises(ValueError):
                Context(example_pix('current'), BASE, bank_payments=(row, second))

    def test_context_rejects_local_overlap_and_current_operation(self):
        row = payment()
        with self.assertRaises(ValueError):
            Context(example_pix('current'), BASE, history=(history(row.pix),), bank_payments=(row,))
        with self.assertRaises(ValueError):
            Context(row.pix, BASE, bank_payments=(row,))

    def test_context_rejects_reused_execution_identity(self):
        row, other = payment('one'), payment('two')
        other = replace(other, execution=replace(other.execution, event_id=row.execution.event_id))
        with self.assertRaises(ValueError):
            Context(example_pix('current'), BASE, bank_payments=(row, other))

    def test_different_providers_can_use_same_operation_identity(self):
        row = payment()
        other = replace(row, provider='another-bank')
        context = Context(example_pix('current'), BASE, bank_payments=(row, other))
        self.assertEqual(len(context.bank_payments), 2)


class KnowledgeClockTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_clock_preserves_receipts_raw_security_and_new_history(self):
        wall = [BASE]
        rows = tuple(payment(str(i), 3+i) for i in range(5))
        rows = tuple(replace(r, execution=replace(r.execution, available_at=BASE+timedelta(seconds=2))) for r in rows)
        class Bank:
            identity = IDENTITY
            async def security(self, scope, as_of, *, timeout_seconds):
                self.requested_at = as_of
                wall[0] += timedelta(seconds=1)
                return security_record(observed_at=wall[0], available_at=wall[0], device_state='revoked')
            async def payments(self, payer, as_of, *, timeout_seconds):
                wall[0] += timedelta(seconds=1)
                return rows
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                wall[0] += timedelta(seconds=1)
                return reputation_record(key.document, observed_at=wall[0], available_at=wall[0], fraud_count=3)
        bank = Bank()
        out = await Enricher(bank, Reputation(), Policy(), clock=lambda: wall[0]).enrich(
            Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
        self.assertEqual(bank.requested_at, BASE)
        self.assertEqual(out.context.decision_at, BASE+timedelta(seconds=3))
        self.assertEqual(dict(out.received_at), {'security':BASE+timedelta(seconds=1),
            'history':BASE+timedelta(seconds=2), 'payer':BASE+timedelta(seconds=3)})
        self.assertEqual(out.security_lookup.snapshot.available_at, BASE+timedelta(seconds=1))
        self.assertEqual(out.security.snapshot, out.security_lookup.snapshot)
        self.assertEqual(out.initial_result, out.lookups[0])
        self.assertTrue(evaluate(out.context).general_profile)
        self.assertEqual(evaluate(out.context).status, 'rejected')

    async def test_future_evidence_still_excluded_and_raw_available_for_revalidation(self):
        future = BASE+timedelta(seconds=1)
        class Bank:
            identity = IDENTITY
            async def security(self, scope, as_of, *, timeout_seconds):
                return security_record(observed_at=future, available_at=future, device_state='revoked')
            async def payments(self, payer, as_of, *, timeout_seconds):
                return ()
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                return reputation_record(key.document, observed_at=future, available_at=future, fraud_count=3)
        out = await Enricher(Bank(), Reputation(), Policy(), clock=lambda: BASE).enrich(
            Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
        self.assertEqual(out.security.state, 'not_covered')
        self.assertEqual(out.security_lookup.snapshot.device_state, 'revoked')
        self.assertEqual(out.initial_result.evidence.state, 'not_covered')
        self.assertTrue(all(e.state == 'not_covered' for _, e in out.context.dict_evidence))
        self.assertTrue(all(r.evidence.fraud_count == 3 for r in out.lookups))

    async def test_security_expiring_during_lookup_requires_refresh_with_stale_material(self):
        from providers.enrichment import EnrichmentNeedsRefresh
        wall = [BASE]
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                wall[0] += timedelta(seconds=1)
                return reputation_record(key.document)
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await Enricher(FixtureBankProvider(IDENTITY, (security_record(valid_until=BASE+timedelta(seconds=1)),)),
                Reputation(), Policy(), clock=lambda: wall[0]).enrich(
                    Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
        partial = caught.exception.partial
        self.assertEqual(partial.security.state, 'stale')
        self.assertFalse(partial.security.snapshot.valid(partial.captured_at))
        self.assertEqual(partial.security_lookup.state, 'available')

    async def test_cache_expiring_during_another_lookup_requires_refresh(self):
        from providers.enrichment import EnrichmentNeedsRefresh
        wall = [BASE]
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                wall[0] += timedelta(seconds=2)
                return reputation_record(key.document)
        with self.assertRaises(EnrichmentNeedsRefresh):
            await Enricher(FixtureBankProvider(IDENTITY), Reputation(), Policy(), clock=lambda: wall[0]).enrich(
                Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3),
                cached=(reputation_record(A, valid_until=BASE+timedelta(seconds=1)),))

    async def test_clock_cannot_precede_input_or_go_backwards(self):
        for stamps in ((BASE-timedelta(seconds=1),), (BASE, BASE-timedelta(seconds=1))):
            values = iter(stamps)
            with self.subTest(stamps=stamps), self.assertRaises(ProviderDataError):
                await Enricher(FixtureBankProvider(IDENTITY), FixtureReputationProvider(IDENTITY),
                    Policy(), clock=lambda: next(values)).enrich(Context(example_pix('current'), BASE),
                        source_check=SourceCheck.OWNED, deadline=Deadline.after(3))

    async def test_security_cache_of_other_scope_is_ignored_like_reputation_cache(self):
        out = await Enricher(FixtureBankProvider(IDENTITY), FixtureReputationProvider(IDENTITY),
            Policy(), clock=lambda: BASE).enrich(Context(example_pix('current'), BASE),
                source_check=SourceCheck.OWNED, deadline=Deadline.after(3),
                security_cached=security_record(replace(example_pix('current'), device_id='different')))
        self.assertEqual(out.security.state, 'not_covered')
        self.assertIsNone(out.context.security)

    async def test_expiry_changing_preliminary_gating_requires_refresh(self):
        from providers.enrichment import EnrichmentNeedsRefresh
        wall = [BASE]
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                wall[0] += timedelta(seconds=2)
                return reputation_record(key.document)
        bank = FixtureBankProvider(IDENTITY, (security_record(valid_until=BASE+timedelta(seconds=1)),),
            tuple(payment(str(i), 3+i) for i in range(5)))
        with self.assertRaises(EnrichmentNeedsRefresh):
            await Enricher(bank, Reputation(), Policy(dict_threshold=90), clock=lambda: wall[0]).enrich(
                Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))

    async def test_unknown_initial_lookup_keeps_raw_failure_and_selected_stale_watermark(self):
        adverse = reputation_record(A, observed_at=BASE-timedelta(minutes=1),
            available_at=BASE-timedelta(minutes=1), valid_until=BASE, fraud_count=2)
        out = await Enricher(FixtureBankProvider(IDENTITY),
            FixtureReputationProvider(IDENTITY, unavailable=(adverse.key,)), Policy(), clock=lambda: BASE).enrich(
                Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED,
                deadline=Deadline.after(3), cached=(adverse,))
        self.assertEqual(out.initial_result.evidence, dict(out.context.dict_evidence)['payer'])
        self.assertEqual(out.initial_result.evidence.state, 'stale')
        self.assertEqual(out.lookups[0].evidence.state, 'unavailable')
        self.assertEqual(next(t for t in out.context.provider_traces if t.role=='payer').lookup_state, 'unavailable')
