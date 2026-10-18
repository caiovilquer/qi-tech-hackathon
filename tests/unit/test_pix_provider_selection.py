"""Provider boundaries and temporal version selection, specified before implementation."""
from dataclasses import replace
from datetime import timedelta
import unittest

from antifraud.context import Context, DictEvidence, SecuritySnapshot, ExecutionEvent
from antifraud.engine import evaluate
from providers.contracts import (ProviderIdentity, SecurityScope, ReputationKey, BankRecord,
                                ReputationRecord, BankPayment, BankLink, ProviderDataError)
from providers.selection import select_security, select_reputation, reconcile_payments
from tests.support.pix_scenarios import BASE, A, B, example_pix
from tests.unit.test_pix_policy_reference import history

IDENTITY = ProviderIdentity('bank-demo', 'v1')


def security_record(p=None, **changes):
    p = p or example_pix('current')
    value = SecuritySnapshot(BASE-timedelta(hours=1), BASE-timedelta(hours=1), BASE+timedelta(minutes=5),
                             'registered')
    return BankRecord(SecurityScope.from_pix(p), IDENTITY, 'available', replace(value, **changes), 'test_bank')


def reputation_record(document=B, tenant='demo', **changes):
    key = ReputationKey(tenant, document, IDENTITY, 'person_reputation', 'pix')
    value = DictEvidence(document, 'available', BASE-timedelta(hours=1), BASE-timedelta(hours=1),
                         BASE+timedelta(minutes=5), provenance='test_dict')
    return ReputationRecord(key, replace(value, **changes))


def payment(op='bank-op', age=3, local_id=None):
    p = example_pix(op, 10000, BASE-timedelta(days=age))
    e = ExecutionEvent('exec:'+op, p.payer, p.id, 'executed', p.transaction_at, p.transaction_at)
    return BankPayment(IDENTITY.name, op, p, e)


class SelectionTests(unittest.TestCase):
    def test_newest_observation_wins_over_late_delivery(self):
        old = security_record(available_at=BASE-timedelta(minutes=1))
        revoked = security_record(observed_at=BASE-timedelta(minutes=10),
                                  available_at=BASE-timedelta(minutes=10), device_state='revoked')
        for items in ((old, revoked), (revoked, old)):
            self.assertEqual(select_security(old.scope, IDENTITY, items, BASE).snapshot.device_state, 'revoked')

    def test_source_version_orders_equal_observations(self):
        old = reputation_record()
        newer = replace(old, evidence=replace(old.evidence, source_version=1, fraud_count=1))
        self.assertEqual(select_reputation(old.key, (newer, old), BASE).evidence.fraud_count, 1)

    def test_future_knowledge_and_observation_are_excluded(self):
        r = reputation_record(available_at=BASE+timedelta(seconds=1))
        self.assertEqual(select_reputation(r.key, (r,), BASE).evidence.state, 'not_covered')
        r = reputation_record(observed_at=BASE+timedelta(seconds=1), available_at=BASE+timedelta(seconds=1))
        self.assertEqual(select_reputation(r.key, (r,), BASE).evidence.state, 'not_covered')

    def test_expired_newest_never_falls_back_to_older_clean(self):
        old = reputation_record()
        newest = reputation_record(observed_at=BASE-timedelta(minutes=1), available_at=BASE-timedelta(minutes=1),
                                   valid_until=BASE, fraud_count=1)
        result = select_reputation(old.key, (old,), BASE, cached=newest)
        self.assertEqual(result.evidence.state, 'stale')
        self.assertEqual(result.evidence.fraud_count, 1)
        self.assertEqual(result.evidence.observed_at, newest.evidence.observed_at)

    def test_expired_revocation_does_not_recover_older_registration(self):
        old = security_record()
        newest = security_record(observed_at=BASE-timedelta(minutes=1), available_at=BASE-timedelta(minutes=1),
                                 valid_until=BASE, device_state='revoked')
        result = select_security(old.scope, IDENTITY, (old,), BASE, cached=newest)
        self.assertEqual(result.state, 'stale')
        self.assertFalse(result.snapshot.valid(BASE))
        self.assertEqual(result.snapshot.device_state, 'revoked')

    def test_conflicting_version_is_error_not_arbitrary_confidence(self):
        old = reputation_record()
        conflict = replace(old, evidence=replace(old.evidence, fraud_count=1))
        with self.assertRaises(ProviderDataError):
            select_reputation(old.key, (old, conflict), BASE)
        old = security_record()
        with self.assertRaises(ProviderDataError):
            select_security(old.scope, IDENTITY, (old, replace(old, snapshot=replace(old.snapshot, device_state='revoked'))), BASE)

    def test_same_fact_delivered_twice_uses_first_knowledge(self):
        first = reputation_record()
        duplicate = replace(first, evidence=replace(first.evidence, available_at=BASE))
        self.assertEqual(select_reputation(first.key, (first, duplicate), BASE).evidence.available_at, first.evidence.available_at)

    def test_later_source_version_can_close_report_or_cancel_marking(self):
        old = reputation_record(fraud_count=1, open_reports=1)
        new = replace(old, evidence=replace(old.evidence, source_version=1, fraud_count=0, open_reports=0))
        result = select_reputation(old.key, (old, new), BASE)
        self.assertEqual((result.evidence.fraud_count, result.evidence.open_reports), (0, 0))

    def test_invalid_temporal_validity_and_subject_rejected_at_boundary(self):
        r = reputation_record()
        for change in ({'observed_at': BASE, 'available_at': BASE-timedelta(seconds=1)},
                       {'valid_until': r.evidence.observed_at}, {'document': A}):
            with self.subTest(change=change), self.assertRaises(ProviderDataError):
                ReputationRecord(r.key, replace(r.evidence, **change))

    def test_wrong_tenant_scope_or_provider_cannot_enter_selection(self):
        r = reputation_record()
        for wrong in (reputation_record(tenant='other'), reputation_record(document=A),
                      replace(r, key=replace(r.key, scope='other-permission')),
                      replace(r, key=replace(r.key, provider=ProviderIdentity('other', 'v1')))):
            with self.subTest(wrong=wrong), self.assertRaises(ProviderDataError):
                select_reputation(r.key, (wrong,), BASE)

    def test_missing_bank_snapshot_has_no_invented_device_registration(self):
        p = example_pix('current')
        record = select_security(SecurityScope.from_pix(p), IDENTITY, (), BASE)
        self.assertEqual(record.state, 'not_covered')
        self.assertIsNone(record.snapshot)


class BankHistoryTests(unittest.TestCase):
    def test_imported_executions_build_profile_without_fake_api_attempts(self):
        c = Context(example_pix('current'), BASE)
        rows = tuple(payment(str(i), 3+i) for i in range(5))
        result = reconcile_payments(c, rows, ())
        self.assertEqual(result.history, ())
        self.assertEqual(len(result.bank_payments), 5)
        decision = evaluate(result)
        self.assertTrue(decision.general_profile)
        self.assertTrue(decision.payee_profile)
        self.assertNotIn('RECEIPT_VELOCITY', {r.code for r in decision.reasons})

    def test_local_match_by_explicit_bank_identity_counts_once_and_imports_execution(self):
        bank = payment()
        local = replace(bank.pix, id='local-id')
        c = Context(example_pix('current'), BASE, history=(history(local),))
        link = BankLink('demo', IDENTITY.name, bank.operation_id, local.id)
        result = reconcile_payments(c, (bank, bank), (link,))
        self.assertEqual(result.bank_payments, ())
        self.assertEqual(len(result.execution_events), 1)
        self.assertEqual(result.execution_events[0].pix_id, 'local-id')
        self.assertTrue(evaluate(result).recipient_established)

    def test_equal_value_time_destination_different_bank_ids_are_distinct(self):
        first, second = payment('one'), payment('two')
        c = reconcile_payments(Context(example_pix('current'), BASE), (first, second), ())
        self.assertEqual(len(c.bank_payments), 2)
        self.assertTrue(evaluate(c).payee_profile)

    def test_ambiguous_local_id_overlap_requires_explicit_link(self):
        bank = payment()
        c = Context(example_pix('current'), BASE, history=(history(bank.pix),))
        with self.assertRaises(ProviderDataError):
            reconcile_payments(c, (bank,), ())

    def test_conflicting_bank_identity_or_local_link_is_rejected(self):
        bank = payment()
        conflict = replace(bank, pix=replace(bank.pix, amount_cents=1))
        with self.assertRaises(ProviderDataError):
            reconcile_payments(Context(example_pix('current'), BASE), (bank, conflict), ())
        local = replace(bank.pix, id='local', amount_cents=1)
        c = Context(example_pix('current'), BASE, history=(history(local),))
        with self.assertRaises(ProviderDataError):
            reconcile_payments(c, (bank,), (BankLink('demo', IDENTITY.name, bank.operation_id, local.id),))

    def test_unconfirmed_failed_execution_cannot_be_imported_as_payment(self):
        row = payment()
        with self.assertRaises(ValueError):
            replace(row, execution=replace(row.execution, state='failed'))

    def test_future_or_late_known_payments_do_not_form_profile(self):
        row = payment()
        row = replace(row, execution=replace(row.execution, available_at=BASE+timedelta(seconds=1)))
        c = reconcile_payments(Context(example_pix('current'), BASE), (row,), ())
        self.assertFalse(evaluate(c).recipient_established)

    def test_other_tenant_or_account_history_is_rejected(self):
        row = payment()
        for payer in (replace(row.pix.payer, tenant='other'), replace(row.pix.payer, account='999')):
            wrong = replace(row, pix=replace(row.pix, payer=payer), execution=replace(row.execution, payer=payer))
            with self.subTest(payer=payer), self.assertRaises(ProviderDataError):
                reconcile_payments(Context(example_pix('current'), BASE), (wrong,), ())


class SelectionSafetyTests(unittest.TestCase):
    def test_same_expired_version_from_provider_and_cached_stale_does_not_conflict(self):
        raw = reputation_record(valid_until=BASE)
        cached = replace(raw, evidence=replace(raw.evidence, state='stale'))
        result = select_reputation(raw.key, (raw,), BASE, cached=cached)
        self.assertEqual(result.evidence.state, 'stale')

    def test_stale_bank_marker_cannot_present_a_valid_registered_snapshot(self):
        raw = replace(security_record(), state='stale')
        with self.assertRaises(ProviderDataError):
            select_security(raw.scope, IDENTITY, (raw,), BASE)

    def test_two_bank_operations_cannot_alias_the_same_local_operation(self):
        one, two = payment('one'), payment('two')
        local = replace(one.pix, id='local')
        links = (BankLink('demo', IDENTITY.name, 'one', 'local'), BankLink('demo', IDENTITY.name, 'two', 'local'))
        with self.assertRaises(ProviderDataError):
            reconcile_payments(Context(example_pix('current'), BASE, history=(history(local),)), (one, two), links)

    def test_reused_execution_identifier_cannot_confirm_two_bank_operations(self):
        one, two = payment('one'), payment('two')
        two = replace(two, execution=replace(two.execution, event_id=one.execution.event_id))
        with self.assertRaises(ProviderDataError):
            reconcile_payments(Context(example_pix('current'), BASE), (one, two), ())

    def test_imported_maturity_requires_both_intention_and_execution(self):
        row = payment(age=2)
        execution = replace(row.execution, occurred_at=BASE-timedelta(hours=24)+timedelta(microseconds=1), available_at=BASE)
        late = replace(row, execution=execution)
        c = reconcile_payments(Context(example_pix('current'), BASE), (late,), ())
        self.assertFalse(evaluate(c).recipient_established)
        mature = replace(row, execution=replace(execution, occurred_at=BASE-timedelta(hours=24)))
        c = reconcile_payments(Context(example_pix('current'), BASE), (mature,), ())
        self.assertTrue(evaluate(c).recipient_established)

    def test_conflicting_bank_provenance_for_same_revision_is_rejected(self):
        row = security_record()
        with self.assertRaises(ProviderDataError):
            select_security(row.scope, IDENTITY, (row, replace(row, provenance='other-origin')), BASE)

    def test_local_history_ownership_change_is_not_silently_mixed(self):
        p = example_pix('current')
        previous = replace(p, id='old-owner', payer=replace(p.payer, document=B))
        with self.assertRaises(ProviderDataError):
            reconcile_payments(Context(p, BASE, history=(history(previous),)), (), ())

    def test_provider_timestamps_are_normalized_to_utc(self):
        from datetime import timezone
        self.assertIs(reputation_record().evidence.observed_at.tzinfo, timezone.utc)
        self.assertIs(security_record().snapshot.observed_at.tzinfo, timezone.utc)
        self.assertIs(payment().execution.occurred_at.tzinfo, timezone.utc)
