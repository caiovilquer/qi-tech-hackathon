"""PR 3 re-review: security freshness and request-local continuation contracts."""
from dataclasses import FrozenInstanceError, replace
from datetime import timedelta
import socket
import ssl
import unittest

from antifraud.context import Context
from antifraud.policy import Policy
from providers.budget import Deadline, ProviderRunner
from providers.contracts import ProviderDataError, ProviderUnavailable
from providers.enrichment import Enricher, EnrichmentNeedsRefresh, SourceCheck
from providers.fixtures import FixtureBankProvider
from tests.unit.test_pix_provider_selection import (
    IDENTITY, BASE, A, B, example_pix, reputation_record, security_record)


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_revocation_or_bank_limit_expiring_during_dict_cannot_return_decision(self):
        for changes in ({'device_state':'revoked'}, {'bank_limit_exceeded':True}, {}):
            wall = [BASE]
            class Slow:
                identity = IDENTITY
                async def reputation(self, key, as_of, *, timeout_seconds):
                    wall[0] += timedelta(seconds=1)
                    return reputation_record(key.document, observed_at=wall[0], available_at=wall[0])
            record = security_record(valid_until=BASE+timedelta(seconds=1), **changes)
            with self.subTest(changes=changes), self.assertRaises(EnrichmentNeedsRefresh):
                await Enricher(FixtureBankProvider(IDENTITY, (record,)), Slow(), Policy(), clock=lambda: wall[0]).enrich(
                    Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))

    async def test_cached_revocation_expiring_during_history_also_requires_refresh(self):
        wall = [BASE]
        class Bank:
            identity = IDENTITY
            async def security(self, scope, as_of, *, timeout_seconds):
                from providers.contracts import BankRecord
                return BankRecord(scope, IDENTITY, 'unavailable', None, 'bank_outage')
            async def payments(self, payer, as_of, *, timeout_seconds):
                wall[0] += timedelta(seconds=2)
                return ()
        cached = security_record(device_state='revoked', valid_until=BASE+timedelta(seconds=1))
        from providers.fixtures import FixtureReputationProvider
        with self.assertRaises(EnrichmentNeedsRefresh):
            await Enricher(Bank(), FixtureReputationProvider(IDENTITY), Policy(), clock=lambda: wall[0]).enrich(
                Context(example_pix('current'), BASE), source_check=SourceCheck.COMPLETED,
                deadline=Deadline.after(3), security_cached=cached)

    async def test_refresh_exposes_immutable_partial_material_and_resumes_origin_once(self):
        wall, calls = [BASE], []
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                calls.append(key.document)
                wall[0] += timedelta(seconds=1)
                return reputation_record(key.document, observed_at=wall[0], available_at=wall[0])
        p, budget = example_pix('current'), Deadline.after(3)
        enricher = Enricher(FixtureBankProvider(IDENTITY, (security_record(valid_until=BASE+timedelta(seconds=1)),)),
            Reputation(), Policy(), clock=lambda: wall[0])
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await enricher.enrich(Context(p, BASE), source_check=SourceCheck.OWNED, deadline=budget)
        partial = caught.exception.partial
        self.assertEqual(partial.security.state, 'stale')
        self.assertEqual(partial.security_lookup.state, 'available')
        self.assertEqual(len(partial.lookups), 2)
        self.assertEqual(partial.initial_result.key.document, A)
        self.assertEqual(partial.initial_result.evidence.state, 'available')
        self.assertEqual({r.key.document for r in partial.cache_updates}, {A, B})
        self.assertEqual({role for role,_ in partial.received_at}, {'security','history','payer','payee'})
        with self.assertRaises(FrozenInstanceError):
            partial.initial_result = None
        # Fresh bank evidence, same providers/permission/policy/request/budget.
        resumed = replace(enricher, bank=FixtureBankProvider(IDENTITY, (security_record(observed_at=wall[0], available_at=wall[0]),)))
        out = await resumed.enrich(Context(p, wall[0]), source_check=SourceCheck.OWNED,
            deadline=budget, resume=partial)
        self.assertEqual(calls.count(A), 1)
        self.assertEqual(calls.count(B), 1)
        self.assertEqual(out.initial_result, partial.initial_result)
        self.assertEqual(out.lookups, partial.lookups)
        self.assertEqual(out.security.state, 'available')
        self.assertGreaterEqual(len(out.received_at), len(partial.received_at)+2)

    async def test_refresh_from_expiring_cache_keeps_first_cached_result(self):
        wall, calls = [BASE], []
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                calls.append(key.document)
                if key.document == A:
                    raise ProviderUnavailable('synthetic outage')
                wall[0] += timedelta(seconds=2)
                return reputation_record(key.document, observed_at=wall[0], available_at=wall[0])
        # Seed payer fact in cache; it expires while querying recipient. Source
        # check completes using cache; a second cycle must not query origin anew.
        p, budget = example_pix('current'), Deadline.after(3)
        enricher = Enricher(FixtureBankProvider(IDENTITY), Reputation(), Policy(), clock=lambda: wall[0])
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await enricher.enrich(Context(p, BASE), source_check=SourceCheck.OWNED, deadline=budget,
                cached=(reputation_record(A, valid_until=BASE+timedelta(seconds=1)),))
        partial = caught.exception.partial
        first = partial.initial_result
        out = await enricher.enrich(Context(p, wall[0]), source_check=SourceCheck.OWNED, deadline=budget, resume=partial)
        self.assertEqual(calls.count(A), 0)
        self.assertEqual(first, out.initial_result)
        self.assertEqual(calls.count(B), 1)

    async def test_same_person_unavailable_lookup_survives_refresh_without_duplicate_calls(self):
        wall, calls = [BASE], []
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                calls.append(key.document)
                wall[0] += timedelta(seconds=1)
                raise ProviderUnavailable('synthetic outage')
        p, budget = example_pix('current', payee=A), Deadline.after(3)
        bank = FixtureBankProvider(IDENTITY, (security_record(p, valid_until=BASE+timedelta(seconds=1)),))
        enricher = Enricher(bank, Reputation(), Policy(), clock=lambda: wall[0])
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await enricher.enrich(Context(p, BASE), source_check=SourceCheck.OWNED, deadline=budget)
        partial = caught.exception.partial
        self.assertEqual(partial.initial_result.evidence.state, 'unavailable')
        self.assertEqual(len(partial.lookups), 1)
        out = await replace(enricher, bank=FixtureBankProvider(IDENTITY, (security_record(p, observed_at=wall[0], available_at=wall[0]),))).enrich(
            Context(p, wall[0]), source_check=SourceCheck.OWNED, deadline=budget, resume=partial)
        self.assertEqual(len(calls), 2)  # one logical lookup + technical retry, across both cycles
        self.assertEqual(out.initial_result, partial.initial_result)
        self.assertEqual(dict(out.context.dict_evidence)['payee'].state, 'unavailable')


class TransportClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_configuration_errors_propagate_once_without_outage(self):
        for error in (FileNotFoundError('CA missing'), PermissionError('CA unreadable'),
                      ssl.SSLError('invalid CA'), OSError('unexpected local failure')):
            calls = []
            async def operation(timeout):
                calls.append(timeout)
                raise error
            with self.subTest(error=type(error)), self.assertRaises(type(error)):
                await ProviderRunner().call(operation, Deadline.after(3))
            self.assertEqual(len(calls), 1)

    async def test_connection_timeout_and_dns_failures_remain_technical(self):
        for error in (ConnectionResetError('reset'), ConnectionRefusedError('refused'),
                      TimeoutError('timeout'), socket.gaierror('DNS unavailable')):
            calls = []
            async def operation(timeout):
                calls.append(timeout)
                raise error
            with self.subTest(error=type(error)), self.assertRaises(ProviderUnavailable):
                await ProviderRunner().call(operation, Deadline.after(3))
            self.assertEqual(len(calls), 2)


class ContinuationBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def make_progress(self, *, unavailable=False, source=SourceCheck.OWNED):
        wall, calls = [BASE], []
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                calls.append(key.document)
                wall[0] += timedelta(seconds=1)
                if unavailable and key.document == A:
                    raise ProviderUnavailable('first origin coverage is unavailable')
                return reputation_record(key.document, observed_at=wall[0], available_at=wall[0])
        p, budget = example_pix('current'), Deadline.after(3)
        enricher = Enricher(FixtureBankProvider(IDENTITY, (security_record(valid_until=BASE+timedelta(seconds=1)),)),
            Reputation(), Policy(), clock=lambda: wall[0])
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await enricher.enrich(Context(p, BASE), source_check=source, deadline=budget)
        return caught.exception.partial, enricher, wall, calls, budget

    async def test_continuation_rejects_other_request_scope_provider_policy_and_budget_before_io(self):
        from providers.contracts import ProviderIdentity
        partial, enricher, wall, calls, budget = await self.make_progress()
        contexts = (Context(replace(partial.pix, id='other-id'), wall[0]),
                    Context(replace(partial.pix, payer=replace(partial.pix.payer, tenant='other')), wall[0]),
                    Context(replace(partial.pix, device_id='other-device'), wall[0]))
        attempts_before = tuple(calls)
        for context in contexts:
            with self.subTest(pix=context.pix), self.assertRaises(ProviderDataError):
                await enricher.enrich(context, source_check=SourceCheck.OWNED, deadline=budget, resume=partial)
        for changed in (replace(enricher, scope='other'), replace(enricher, policy=Policy(dict_threshold=79)),
                        replace(enricher, bank=FixtureBankProvider(ProviderIdentity('other','v1'))),
                        replace(enricher, reputation=replace_identity(ProviderIdentity('other','v1')))):
            with self.subTest(changed=changed), self.assertRaises(ProviderDataError):
                await changed.enrich(Context(partial.pix, wall[0]), source_check=SourceCheck.OWNED,
                    deadline=budget, resume=partial)
        with self.assertRaises(ProviderDataError):
            await enricher.enrich(Context(partial.pix, wall[0]), source_check=SourceCheck.OWNED,
                deadline=Deadline.after(3), resume=partial)
        self.assertEqual(tuple(calls), attempts_before)

    async def test_unavailable_origin_is_not_replaced_after_refresh_or_completed_transition(self):
        partial, enricher, wall, calls, budget = await self.make_progress(unavailable=True)
        self.assertEqual(partial.initial_result.evidence.state, 'unavailable')
        self.assertEqual(calls.count(A), 2)  # technical retry, not two logical lookups
        initial = partial.initial_result
        fresh = security_record(observed_at=wall[0], available_at=wall[0])
        out = await replace(enricher, bank=FixtureBankProvider(IDENTITY, (fresh,))).enrich(
            Context(partial.pix, wall[0]), source_check=SourceCheck.COMPLETED, deadline=budget, resume=partial)
        self.assertEqual(calls.count(A), 2)
        self.assertEqual(out.initial_result, initial)
        self.assertEqual(dict(out.context.dict_evidence)['payer'].state, 'unavailable')
        self.assertEqual(out.lookups, partial.lookups)
        self.assertIs(out.progress.deadline, budget)

    async def test_completed_source_cannot_reopen_from_progress(self):
        partial, enricher, wall, calls, budget = await self.make_progress(source=SourceCheck.COMPLETED)
        before = tuple(calls)
        with self.assertRaises(ProviderDataError):
            await enricher.enrich(Context(partial.pix, wall[0]), source_check=SourceCheck.OWNED,
                deadline=budget, resume=partial)
        self.assertEqual(tuple(calls), before)

    async def test_already_stale_security_cannot_return_a_context(self):
        from providers.fixtures import FixtureReputationProvider
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await Enricher(FixtureBankProvider(IDENTITY, (security_record(valid_until=BASE),)),
                FixtureReputationProvider(IDENTITY), Policy(), clock=lambda: BASE).enrich(
                    Context(example_pix('current'), BASE), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
        self.assertEqual(caught.exception.partial.security.state, 'stale')

    async def test_resume_does_not_reset_an_exhausted_deadline(self):
        from providers.budget import BudgetExceeded
        partial, enricher, wall, calls, original = await self.make_progress()
        tick = [0.0]
        budget = Deadline(1, lambda: tick[0])
        partial = replace(partial, deadline=budget)
        tick[0] = 2
        before = tuple(calls)
        with self.assertRaises(BudgetExceeded):
            await enricher.enrich(Context(partial.pix, wall[0]), source_check=SourceCheck.OWNED,
                deadline=budget, resume=partial)
        self.assertEqual(tuple(calls), before)


def replace_identity(identity):
    class OtherIdentity:
        async def reputation(self, key, as_of, *, timeout_seconds):
            raise AssertionError('Mismatched continuation must fail before providers')
    result = OtherIdentity()
    result.identity = identity
    return result


class ContinuationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_same_person_is_not_requeried_when_short_ttl_expires_between_cycles(self):
        wall, calls = [BASE], []
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                calls.append(key.document)
                wall[0] += timedelta(seconds=1)
                raise ProviderUnavailable('unknown throughout this request')
        p, budget = example_pix('current', payee=A), Deadline.after(3)
        enricher = Enricher(FixtureBankProvider(IDENTITY, (security_record(p, valid_until=BASE+timedelta(seconds=1)),)),
            Reputation(), Policy(dict_ttl_seconds=1), clock=lambda: wall[0])
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await enricher.enrich(Context(p, BASE), source_check=SourceCheck.OWNED, deadline=budget)
        partial = caught.exception.partial
        wall[0] += timedelta(seconds=2)
        fresh = security_record(p, observed_at=wall[0], available_at=wall[0])
        out = await replace(enricher, bank=FixtureBankProvider(IDENTITY, (fresh,))).enrich(
            Context(p, wall[0]), source_check=SourceCheck.OWNED, deadline=budget, resume=partial)
        self.assertEqual(len(calls), 2)
        self.assertEqual(out.initial_result, partial.initial_result)
        self.assertEqual(dict(out.context.dict_evidence)['payee'].state, 'unavailable')

    async def test_mixed_subject_or_tenant_in_partial_is_rejected_before_providers(self):
        helper = ContinuationBoundaryTests()
        partial, enricher, wall, calls, budget = await helper.make_progress()
        for changed in (replace(partial, initial_result=reputation_record(B)),
                        replace(partial, selected=(reputation_record(tenant='other'),)),
                        replace(partial, lookups=(reputation_record(tenant='other'),))):
            before = tuple(calls)
            with self.subTest(changed=changed), self.assertRaises(ProviderDataError):
                await enricher.enrich(Context(partial.pix, wall[0]), source_check=SourceCheck.OWNED,
                    deadline=budget, resume=changed)
            self.assertEqual(tuple(calls), before)

    async def test_resume_with_higher_confidence_does_not_require_old_failed_recipient_lookup(self):
        wall, calls = [BASE], []
        class Reputation:
            identity = IDENTITY
            async def reputation(self, key, as_of, *, timeout_seconds):
                calls.append(key.document)
                wall[0] += timedelta(seconds=1)
                raise ProviderUnavailable('recipient was unavailable')
        p, budget = example_pix('current'), Deadline.after(3)
        enricher = Enricher(FixtureBankProvider(IDENTITY, (security_record(valid_until=BASE+timedelta(seconds=1)),)),
            Reputation(), Policy(), clock=lambda: wall[0])
        with self.assertRaises(EnrichmentNeedsRefresh) as caught:
            await enricher.enrich(Context(p, BASE), source_check=SourceCheck.COMPLETED, deadline=budget)
        partial = caught.exception.partial
        from tests.unit.test_pix_provider_selection import payment
        bank = FixtureBankProvider(IDENTITY, (security_record(observed_at=wall[0], available_at=wall[0]),),
            tuple(payment(str(i), 3+i) for i in range(5)))
        out = await replace(enricher, bank=bank).enrich(Context(p, wall[0]),
            source_check=SourceCheck.COMPLETED, deadline=budget, resume=partial)
        self.assertEqual(len(calls), 2)
        self.assertEqual(dict(out.context.dict_actions)['payee'], 'skipped_confidence')
        self.assertEqual(out.context.dict_queried, ())
