"""Pure engine, exact boundary adapter, safe DTO and audit regressions."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import builtins
import socket
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from antifraud.adapter import to_pix
from antifraud.context import Context, DictEvidence, SecuritySnapshot, TWO_FA, REJECTED
from antifraud.contract import normalize_payload
from antifraud.engine import evaluate, preliminary
from antifraud.features import build_features, median
from antifraud.policy import Policy
from antifraud.dto import PixTransactionDTO
from tests.unit.test_pix_contract import OFFICIAL_PAYLOAD
from tests.unit.test_pix_policy_reference import history, execution, security
from tests.support.pix_scenarios import BASE

KEY = '58ce6c65-0d45-4d5f-b5c8-2e65585a3fb8'


def snapshot(key, normalized, context, policy):
    return PixTransactionDTO.from_decision(key, normalized, context, policy, evaluate(context, policy))


class PixFixture:

    def setUp(self):
        self.normalized = normalize_payload(deepcopy(OFFICIAL_PAYLOAD))
        self.pix = to_pix(self.normalized, 'demo')
        self.context = Context(self.pix, self.pix.transaction_at)
        self.policy = Policy()


class EngineTests(PixFixture, unittest.TestCase):
    def test_official_adapter_keeps_cents_identity_and_server_tenant(self):
        self.assertEqual(self.pix.id, 'pix-000123')
        self.assertEqual(self.pix.amount_cents, 150000)
        self.assertIs(type(self.pix.amount_cents), int)
        self.assertEqual(self.pix.payer.tenant, 'demo')
        self.assertEqual(self.pix.payer.document, '12345678909')
        self.assertEqual(self.pix.transaction_at.utcoffset(), timedelta(0))
        self.assertEqual(self.pix.destination.participant, '60701190')
        self.assertEqual(self.pix.device_id, 'dvc-9f2c71a8')
        self.assertNotEqual(self.pix.payer, to_pix(self.normalized, 'other').payer)
        with self.assertRaises(ValueError):
            to_pix(self.normalized, '')

    def test_normalized_payload_cannot_be_changed_through_returned_map(self):
        payload = self.normalized.canonical_payload
        payload['amount'] = 1
        payload['source']['device_id'] = 'changed'
        self.assertEqual(to_pix(self.normalized, 'demo'), self.pix)
        with self.assertRaises(FrozenInstanceError):
            self.normalized.payload_hash = 'changed'

    def test_domain_rejects_mutable_collections_and_naive_times(self):
        with self.assertRaises(TypeError):
            replace(self.context, history=[])
        with self.assertRaises(TypeError):
            SecuritySnapshot(BASE, BASE, BASE + timedelta(days=1), recoveries=[])
        with self.assertRaises(TypeError):
            SecuritySnapshot(BASE, BASE, BASE + timedelta(days=1), restrictions=(object(),))
        with self.assertRaises(TypeError):
            SecuritySnapshot(BASE, BASE, BASE + timedelta(days=1), recoveries=((BASE, 'bad-time'),))
        with self.assertRaises(ValueError):
            replace(self.pix, transaction_at=datetime(2026, 10, 17))
        for amount in (True, 0, -1, 1.5):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                replace(self.pix, amount_cents=amount)

    def test_domain_rejects_incorrect_reputation_subject_and_duplicate_roles(self):
        evidence = DictEvidence(self.pix.payee_document, 'available', BASE, BASE, BASE+timedelta(days=1), provenance="synthetic_fixture")
        with self.assertRaises(ValueError):
            replace(self.context, dict_evidence=(('payer', evidence),))
        with self.assertRaises(ValueError):
            replace(self.context, dict_evidence=(('payee', evidence), ('payee', evidence)))
        with self.assertRaises(ValueError):
            replace(self.context, dict_queried=('payer', 'payer'))
        with self.assertRaises(ValueError):
            replace(evidence, fraud_count=-1)
        with self.assertRaises(ValueError):
            replace(evidence, source_version=True)

    def test_missing_required_dict_evidence_is_uncertainty_once(self):
        decision = evaluate(replace(self.context, dict_queried=('payer', 'payee')))
        missing = [r for r in decision.reasons if r.code == 'DICT_CHECK_UNAVAILABLE']
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0].points, 10)
        self.assertNotIn('DICT_CHECK_UNAVAILABLE', {r.code for r in evaluate(self.context).reasons})

    def test_unavailable_notcovered_stale_and_future_are_not_clean(self):
        for state, observed, expires in (
            ('not_covered', BASE, BASE+timedelta(days=1)),
            ('unavailable', BASE, BASE+timedelta(days=1)),
            ('stale', BASE, BASE+timedelta(days=1)),
            ('available', BASE, self.context.decision_at),
            ('available', self.context.decision_at+timedelta(seconds=1), BASE+timedelta(days=10)),
        ):
            with self.subTest(state=state, observed=observed):
                evidence = DictEvidence(self.pix.payer.document, state, observed, BASE, expires, provenance="synthetic_fixture")
                c = replace(self.context, dict_evidence=(('payer', evidence),), dict_queried=('payer',))
                self.assertIn('DICT_CHECK_UNAVAILABLE', {r.code for r in evaluate(c).reasons})

    def test_preliminary_uses_exact_same_local_rules_under_custom_weights(self):
        config = self.policy.configuration()
        config['criteria']['LOW_HISTORY_EXPOSURE']['low_points'] = 22
        from antifraud.configuration import PolicyCatalog
        policy = PolicyCatalog.from_mapping({'policies': {'custom': config}, 'tenants': {'demo': 'custom'}}).for_tenant('demo')
        evidence = DictEvidence(self.pix.payer.document, 'available', BASE, BASE,
                                BASE+timedelta(days=10), fraud_count=1, provenance="synthetic_fixture")
        context = replace(self.context, dict_evidence=(('payer', evidence),), dict_queried=('payer',))
        before, after = preliminary(context, policy), evaluate(context, policy)
        self.assertEqual(before.local_score, after.local_score)
        self.assertEqual(before.c_pre, after.c_pre)
        self.assertEqual(before.score, before.local_score)
        self.assertGreater(after.score, before.score)
        self.assertEqual(next(r.points for r in after.reasons if r.code == 'LOW_HISTORY_EXPOSURE'), 22)

    def test_zero_weights_and_caps_cannot_remove_rejection_floor(self):
        from antifraud.configuration import PolicyCatalog
        settings = self.policy.configuration()
        settings['parameters']['family_caps'] = {key: 0 for key, _ in self.policy.family_caps}
        for args in settings['criteria'].values():
            for key in args:
                args[key] = 0
        policy = PolicyCatalog.from_mapping({'policies': {'zeros': settings}, 'tenants': {'demo': 'zeros'}}).for_tenant('demo')
        c = replace(self.context, security=security(self.pix, 'revoked'))
        d = evaluate(c, policy)
        self.assertEqual(d.score, 0)
        self.assertEqual(d.status, REJECTED)
        self.assertEqual(next(r.floor for r in d.reasons if r.code == 'DEVICE_REVOKED'), REJECTED)
        # Device limit floor is independent of points as well.
        d = evaluate(replace(c, security=security(self.pix, 'unregistered'),
                             pix=replace(self.pix, transaction_at=BASE.replace(hour=14))), policy)
        self.assertEqual(d.status, TWO_FA)

    def test_exact_medians_even_values_and_large_cent_values(self):
        self.assertEqual(median([100000000000, 99999999999]), Fraction(199999999999, 2))
        self.assertEqual(median([3, 1, 2]), Fraction(2))
        self.assertIsInstance(median([1, 2]), Fraction)

    def test_arithmetic_near_minimum_utc_year_with_negative_offset(self):
        # Local year 1, UTC year 2; duration windows must operate in UTC.
        t = datetime(1, 12, 31, 23, 59, tzinfo=timezone(-timedelta(hours=1)))
        pix = replace(self.pix, transaction_at=t, destination_opened_at=t)
        self.assertIn(evaluate(Context(pix, t), replace(self.policy, history_days=365)).status,
                      ('approved', TWO_FA, REJECTED))

    def test_24_hour_duration_across_historical_dst_uses_elapsed_time(self):
        zone = ZoneInfo('America/Sao_Paulo')
        t = datetime(2018, 11, 5, 0, tzinfo=zone)
        pix = replace(self.pix, transaction_at=t, destination_opened_at=datetime(2018, 1, 1, tzinfo=zone))
        prior = replace(pix, id='before-dst', transaction_at=datetime(2018, 11, 3, 23, 30, tzinfo=zone))
        # 24h30 on the civil clock, but only 23h30 elapsed after the DST jump.
        context = Context(pix, t, history=(history(prior),))
        features = build_features(context, self.policy)
        self.assertEqual(features.attempts_sum, 2 * pix.amount_cents)
        self.assertEqual(len(features.day), 1)

    def test_engine_has_no_io_or_global_clock_and_does_not_mutate_inputs(self):
        original = deepcopy(self.context)
        with (patch.object(builtins, 'open', side_effect=AssertionError('file I/O')),
              patch.object(socket.socket, 'connect', side_effect=AssertionError('network'))):
            first = evaluate(self.context, self.policy)
            second = evaluate(self.context, self.policy)
        self.assertEqual(first, second)
        self.assertEqual(self.context, original)
        with self.assertRaises(FrozenInstanceError):
            first.status = 'approved'


class SnapshotTests(PixFixture, unittest.TestCase):
    def test_public_top_level_has_only_contract_keys_and_no_sensitive_facts(self):
        item = DictEvidence(self.pix.payee_document, 'available', BASE, BASE,
                            BASE+timedelta(days=10), fraud_count=7, provenance='private-bureau')
        c = replace(self.context, dict_evidence=(('payee', item),), dict_queried=('payee',),
                    dict_actions=(('payee', 'queried'),))
        dto = snapshot(KEY, self.normalized, c, self.policy)
        self.assertEqual(set(dto.public), {'key', 'status', 'metadata'})
        for secret in (self.pix.payee_document, 'fraud_count', 'private-bureau'):
            self.assertNotIn(secret, dto.public_json)
        self.assertEqual(dto.public['metadata']['enrichment']['payee'], {'action': 'queried', 'state': 'available'})
        self.assertEqual(dto.audit['dict_evidence']['payee']['fraud_count'], 7)
        self.assertEqual(dto.audit['policy']['fingerprint'], self.policy.fingerprint)
        self.assertEqual(dto.audit['decision']['status'], dto.public['status'])

    def test_frozen_dto_survives_mutation_event_and_policy_change(self):
        dto = snapshot(KEY, self.normalized, self.context, self.policy)
        public, audit = dto.public_json, dto.audit_json
        dto.public['metadata']['reasons'].clear()
        dto.audit['policy']['parameters']['family_caps'].clear()
        c = replace(self.context, security=security(self.pix, 'revoked'))
        new = snapshot(KEY, self.normalized, c, replace(self.policy, version='next-version'))
        self.assertNotEqual(new.public_json, public)
        self.assertEqual((dto.public_json, dto.audit_json), (public, audit))
        with self.assertRaises(FrozenInstanceError):
            dto.public_json = '{}'

    def test_snapshot_rejects_mismatched_payload_and_invalid_key(self):
        for context in (replace(self.context, pix=replace(self.pix, amount_cents=1)),
                        replace(self.context, pix=replace(self.pix, id='another'))):
            with self.assertRaises(ValueError):
                snapshot(KEY, self.normalized, context, self.policy)
        with self.assertRaises(ValueError):
            snapshot('bad-key', self.normalized, self.context, self.policy)

    def test_missing_and_skipped_enrichment_have_distinct_states(self):
        c = replace(self.context, dict_queried=('payer',), dict_actions=(('payee', 'skipped_confidence'),))
        dto = snapshot(KEY, self.normalized, c, self.policy)
        self.assertEqual(dto.public['metadata']['enrichment']['payer'], {'action': 'queried', 'state': 'unavailable'})
        self.assertEqual(dto.public['metadata']['enrichment']['payee'], {'action': 'skipped_confidence', 'state': None})

    def test_audit_preserves_fractional_cent_median_without_float(self):
        old1 = replace(self.pix, id='old1', amount_cents=1,
                       transaction_at=self.pix.transaction_at-timedelta(days=3))
        old2 = replace(old1, id='old2', amount_cents=2, transaction_at=old1.transaction_at-timedelta(days=1))
        c = replace(self.context, history=(history(old1), history(old2)),
                    execution_events=(execution(old1), execution(old2)))
        dto = snapshot(KEY, self.normalized, c, self.policy)
        self.assertEqual(dto.audit['features']['profile_median_cents'], {'numerator': 3, 'denominator': 2})
        self.assertEqual(dto.audit['features']['mature_payee_payments'], 2)
