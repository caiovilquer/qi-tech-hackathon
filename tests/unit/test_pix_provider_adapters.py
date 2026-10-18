"""Adapters and request/provider deadlines: independent behavioral contracts."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import unittest

from providers.budget import Deadline, ProviderRunner, BudgetExceeded
from providers.contracts import ProviderUnavailable, ProviderDataError
from providers.fixtures import FixtureBankProvider, FixtureReputationProvider
from tests.unit.test_pix_provider_selection import (IDENTITY, BASE, B, example_pix,
                                                   security_record, reputation_record, payment)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bank_security_and_history_have_separate_methods_and_exact_scope(self):
        p = example_pix('current')
        rec = security_record(p)
        row = payment()
        provider = FixtureBankProvider(IDENTITY, (rec,), (row,))
        result = await provider.security(rec.scope, BASE, timeout_seconds=.25)
        self.assertEqual(result, rec)
        self.assertEqual(await provider.payments(p.payer, BASE, timeout_seconds=.25), (row,))
        for wrong in (replace(rec.scope, device_id='other'), replace(rec.scope, payer=replace(p.payer, tenant='other')),
                      replace(rec.scope, destination=replace(p.destination, account='999'))):
            missing = await provider.security(wrong, BASE, timeout_seconds=.25)
            self.assertEqual(missing.state, 'not_covered')
            self.assertIsNone(missing.snapshot)

    async def test_reputation_missing_is_notcovered_and_expiry_is_stale(self):
        row = reputation_record()
        provider = FixtureReputationProvider(IDENTITY, (row,))
        self.assertEqual(await provider.reputation(row.key, BASE, timeout_seconds=.25), row)
        expired = await provider.reputation(row.key, BASE+timedelta(minutes=6), timeout_seconds=.25)
        self.assertEqual(expired.evidence.state, 'stale')
        other = await provider.reputation(replace(row.key, tenant='other'), BASE, timeout_seconds=.25)
        self.assertEqual(other.evidence.state, 'not_covered')
        self.assertEqual(other.key.tenant, 'other')

    async def test_provider_dataset_is_copied_and_outputs_are_immutable(self):
        data = [reputation_record()]
        provider = FixtureReputationProvider(IDENTITY, data)
        data.clear()
        result = await provider.reputation(reputation_record().key, BASE, timeout_seconds=.25)
        self.assertEqual(result.evidence.state, 'available')
        with self.assertRaises(AttributeError):
            result.evidence.fraud_count = 1

    async def test_foreign_provider_is_rejected_in_dataset_and_request(self):
        from providers.contracts import ProviderIdentity
        foreign = ProviderIdentity('other', 'v1')
        with self.assertRaises(ProviderDataError):
            FixtureReputationProvider(foreign, (reputation_record(),))
        provider = FixtureReputationProvider(IDENTITY, ())
        with self.assertRaises(ProviderDataError):
            await provider.reputation(replace(reputation_record().key, provider=foreign), BASE, timeout_seconds=.25)

    async def test_bank_history_filters_tenant_account_and_knowledge(self):
        row = payment()
        provider = FixtureBankProvider(IDENTITY, (), (row,))
        self.assertEqual(await provider.payments(replace(row.pix.payer, tenant='other'), BASE, timeout_seconds=.25), ())
        self.assertEqual(await provider.payments(row.pix.payer, row.pix.transaction_at-timedelta(seconds=1), timeout_seconds=.25), ())

    async def test_bank_dataset_rejects_identity_reuse_across_accounts(self):
        row = payment()
        payer = replace(row.pix.payer, account='999')
        other = replace(row, pix=replace(row.pix, payer=payer), execution=replace(row.execution, payer=payer))
        with self.assertRaises(ProviderDataError):
            FixtureBankProvider(IDENTITY, (), (row, other))

    async def test_fixture_unavailable_is_explicit_and_does_not_invent_clean_evidence(self):
        rec = reputation_record()
        provider = FixtureReputationProvider(IDENTITY, (), unavailable=(rec.key,))
        with self.assertRaises(ProviderUnavailable):
            await provider.reputation(rec.key, BASE, timeout_seconds=.25)
        bank = security_record()
        provider = FixtureBankProvider(IDENTITY, (), (), unavailable=(bank.scope,))
        with self.assertRaises(ProviderUnavailable):
            await provider.security(bank.scope, BASE, timeout_seconds=.25)

    async def test_adapter_rejects_nonpositive_or_nonfinite_timeout(self):
        provider = FixtureReputationProvider(IDENTITY, ())
        for timeout in (0, -.1, float('nan'), float('inf'), True):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                await provider.reputation(reputation_record().key, BASE, timeout_seconds=timeout)


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_fractional_timeout_preserved_and_request_remaining_limits_call(self):
        seen = []
        async def call(timeout):
            seen.append(timeout)
            return 'ok'
        runner = ProviderRunner()
        self.assertEqual(await runner.call(call, Deadline.after(3)), 'ok')
        self.assertEqual(seen[0], .25)
        self.assertEqual(await runner.call(call, Deadline.after(.1)), 'ok')
        self.assertGreater(seen[1], 0)
        self.assertLessEqual(seen[1], .1)

    async def test_only_one_retry_and_technical_failure_is_distinct_from_missing(self):
        calls = []
        async def call(timeout):
            calls.append(timeout)
            raise ProviderUnavailable('temporary')
        with self.assertRaises(ProviderUnavailable):
            await ProviderRunner().call(call, Deadline.after(3))
        self.assertEqual(len(calls), 2)

    async def test_transient_failure_then_success_uses_same_budget(self):
        calls = []
        async def call(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                raise ProviderUnavailable('temporary')
            return 'recovered'
        self.assertEqual(await ProviderRunner().call(call, Deadline.after(3)), 'recovered')
        self.assertEqual(len(calls), 2)

    async def test_invalid_data_is_not_retried(self):
        calls = []
        async def call(timeout):
            calls.append(timeout)
            raise ProviderDataError('wrong subject')
        with self.assertRaises(ProviderDataError):
            await ProviderRunner().call(call, Deadline.after(3))
        self.assertEqual(len(calls), 1)

    async def test_cooperative_timeout_cancels_each_attempt(self):
        cancelled = []
        async def blocked(timeout):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        with self.assertRaises(ProviderUnavailable):
            await asyncio.wait_for(ProviderRunner(call_seconds=.01).call(blocked, Deadline.after(.5)), 1)
        self.assertEqual(len(cancelled), 2)

    async def test_caller_cancellation_is_not_converted_to_unavailable_or_retried(self):
        entered = asyncio.Event()
        calls = []
        async def blocked(timeout):
            calls.append(timeout)
            entered.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(ProviderRunner().call(blocked, Deadline.after(3)))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(calls), 1)

    async def test_expired_request_never_calls_provider(self):
        async def call(timeout):
            self.fail('must not call')
        with self.assertRaises(BudgetExceeded):
            await ProviderRunner().call(call, Deadline(1, lambda: 2))

    async def test_late_result_is_discarded_and_total_deadline_prevents_retry(self):
        now = [0.0]
        calls = []
        async def call(timeout):
            calls.append(timeout)
            now[0] = 4
            return 'late approval evidence'
        with self.assertRaises(BudgetExceeded):
            await ProviderRunner().call(call, Deadline(3, lambda: now[0]))
        self.assertEqual(len(calls), 1)

    async def test_late_call_result_before_total_deadline_is_not_accepted(self):
        now = [0.0]
        calls = []
        async def call(timeout):
            calls.append(timeout)
            now[0] += .3
            return 'too late for call'
        with self.assertRaises(ProviderUnavailable):
            await ProviderRunner().call(call, Deadline(3, lambda: now[0]))
        self.assertEqual(len(calls), 2)

    async def test_invalid_budget_configuration_rejected(self):
        for value in (0, -1, True, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ProviderRunner(call_seconds=value)
        with self.assertRaises(ValueError):
            ProviderRunner(attempts=3)
        with self.assertRaises(ValueError):
            Deadline.after(0)


class OwnershipTests(unittest.TestCase):
    def test_bank_fixture_detects_account_ownership_change(self):
        first = security_record()
        changed = replace(first, scope=replace(first.scope, payer=replace(first.scope.payer, document=B)))
        with self.assertRaises(ProviderDataError):
            FixtureBankProvider(IDENTITY, (first, changed))
