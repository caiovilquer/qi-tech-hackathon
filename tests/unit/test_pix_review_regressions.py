"""Independent regressions for PR 2 review; written before the fixes."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from dotenv import dotenv_values

from antifraud.configuration import PolicyCatalog
from antifraud.context import (Context, DictEvidence, Pix, Restriction,
                               SecuritySnapshot, APPROVED, TWO_FA, REJECTED)
from antifraud import contract
from antifraud.contract import normalize_payload, ValidationError
from antifraud.dto import PixTransactionDTO
from antifraud.engine import evaluate, preliminary
from antifraud.policy import Policy
from tests.support.pix_scenarios import A, B, BASE, example_pix
from tests.unit.test_pix_contract import OFFICIAL_PAYLOAD
from tests.unit.test_pix_engine import snapshot
from tests.unit.test_pix_policy_reference import history

ROOT = Path(__file__).resolve().parents[2]
KEY = '58ce6c65-0d45-4d5f-b5c8-2e65585a3fb8'


def configured(**weights):
    settings = Policy().configuration()
    for code, points in weights.items():
        settings['criteria'][code]['points'] = points
    return PolicyCatalog.from_mapping({'policies': {'review': settings}, 'tenants': {'demo': 'review'}}).for_tenant('demo')


def evidence(document, **kwargs):
    return DictEvidence(document, 'available', BASE-timedelta(minutes=1), BASE-timedelta(minutes=1),
                        BASE+timedelta(minutes=5), provenance='review_fixture', **kwargs)


class UniqueContributionTests(unittest.TestCase):
    def test_velocity_maximum_for_asymmetric_weights_and_each_clock_alone(self):
        p = example_pix('current')
        for burst, receipt in ((10, 30), (30, 10), (0, 30), (30, 0), (0, 0), (10, 10), (60, 90)):
            policy = configured(VELOCITY_BURST=burst, RECEIPT_VELOCITY=receipt)
            for burst_hit, receipt_hit in ((True, True), (True, False), (False, True), (False, False)):
                with self.subTest(burst=burst, receipt=receipt, clocks=(burst_hit, receipt_hit)):
                    items = tuple(history(replace(p, id=str(i), transaction_at=BASE-timedelta(minutes=1 if burst_hit else 11)),
                        decision_at=BASE-timedelta(minutes=1 if receipt_hit else 11)) for i in range(4))
                    c = Context(p, BASE, items)
                    d = evaluate(c, policy)
                    expected = max(burst if burst_hit else 0, receipt if receipt_hit else 0)
                    self.assertEqual(sum(r.points for r in d.reasons if r.family == 'Velocity'), expected)
                    self.assertEqual(dict(d.families)['Velocity'], min(30, expected))
                    self.assertEqual(d.status, TWO_FA if expected >= 30 else APPROVED)
                    self.assertEqual(preliminary(c, policy).local_score, d.local_score)
                    self.assertEqual(preliminary(c, policy).c_pre, d.c_pre)

    def test_both_velocity_floors_survive_zero_weights(self):
        p = example_pix('current')
        policy = configured(VELOCITY_BURST=0, RECEIPT_VELOCITY=0)
        for burst_hit, receipt_hit in ((True, False), (False, True), (True, True)):
            with self.subTest(clocks=(burst_hit, receipt_hit)):
                items = tuple(history(replace(p, id=str(i), transaction_at=BASE-timedelta(minutes=1 if burst_hit else 11)),
                    decision_at=BASE-timedelta(minutes=1 if receipt_hit else 11)) for i in range(9))
                d = evaluate(Context(p, BASE, items), policy)
                self.assertEqual(d.status, REJECTED)
                self.assertEqual(dict(d.families)['Velocity'], 0)

    def test_adverse_same_document_uses_maximum_once_with_asymmetric_weights(self):
        p = example_pix('current', payee=A)
        for payer_weight, payee_weight in ((0, 30), (30, 0), (10, 30), (30, 10), (0, 0), (60, 90)):
            policy = configured(PAYER_DICT_ADVERSE=payer_weight, PAYEE_DICT_ADVERSE=payee_weight)
            for roles in (('payer', 'payee'), ('payee', 'payer')):
                with self.subTest(weights=(payer_weight, payee_weight), roles=roles):
                    c = Context(p, BASE, dict_evidence=tuple((role, evidence(A, fraud_count=1)) for role in roles))
                    d = evaluate(c, policy)
                    self.assertEqual(sum(r.points for r in d.reasons if r.code.endswith('_DICT_ADVERSE')),
                                     max(payer_weight, payee_weight))
                    self.assertEqual(dict(d.families)['Contraparte'], min(50, max(payer_weight, payee_weight)))

    def test_distinct_adverse_documents_keep_independent_contributions(self):
        p = example_pix('current')
        c = Context(p, BASE, dict_evidence=(('payer', evidence(A, fraud_count=1)), ('payee', evidence(B, fraud_count=1))))
        d = evaluate(c, configured(PAYER_DICT_ADVERSE=10, PAYEE_DICT_ADVERSE=30))
        self.assertEqual(dict(d.families)['Contraparte'], 40)

    def test_same_document_with_only_one_valid_adverse_role_uses_its_weight(self):
        p = example_pix('current', payee=A)
        policy = configured(PAYER_DICT_ADVERSE=10, PAYEE_DICT_ADVERSE=30)
        for stale_role in ('payer', 'payee'):
            with self.subTest(stale_role=stale_role):
                items = tuple((role, replace(evidence(A, fraud_count=1),
                    valid_until=BASE if role == stale_role else BASE+timedelta(minutes=1))) for role in ('payer', 'payee'))
                d = evaluate(Context(p, BASE, dict_evidence=items), policy)
                self.assertEqual(dict(d.families)['Contraparte'], 30 if stale_role == 'payer' else 10)


class RecoveryBoundaryTests(unittest.TestCase):
    def context(self, recoveries, expires=None):
        p = example_pix('current')
        sec = SecuritySnapshot(BASE-timedelta(days=2), BASE-timedelta(days=2), expires or BASE+timedelta(days=1),
                               'registered', recoveries=tuple(recoveries))
        return Context(p, BASE, security=sec)

    def points(self, c):
        return [r.points for r in evaluate(c).reasons if r.code == 'RECENT_ACCOUNT_RECOVERY']

    def test_left_boundary_is_open_and_one_microsecond_inside_counts(self):
        cutoff = BASE.astimezone(timezone.utc)-timedelta(hours=24)
        self.assertEqual(self.points(self.context(((cutoff, cutoff),))), [])
        inside = cutoff+timedelta(microseconds=1)
        self.assertEqual(self.points(self.context(((inside, inside),))), [25])

    def test_right_boundary_is_inclusive_and_future_occurrence_is_ignored(self):
        self.assertEqual(self.points(self.context(((BASE, BASE),))), [25])
        self.assertEqual(self.points(self.context(((BASE+timedelta(microseconds=1), BASE),))), [])

    def test_knowledge_after_decision_does_not_apply(self):
        self.assertEqual(self.points(self.context(((BASE-timedelta(hours=1), BASE+timedelta(microseconds=1)),))), [])
        self.assertEqual(self.points(self.context(((BASE-timedelta(hours=1), BASE),))), [25])

    def test_recovery_has_its_own_time_window_even_if_snapshot_expired(self):
        self.assertEqual(self.points(self.context(((BASE-timedelta(hours=1), BASE),), expires=BASE)), [25])

    def test_multiple_recoveries_count_once_and_custom_weight_is_respected(self):
        c = self.context(((BASE, BASE), (BASE-timedelta(hours=1), BASE)))
        self.assertEqual(self.points(c), [25])
        d = evaluate(c, configured(RECENT_ACCOUNT_RECOVERY=17))
        self.assertEqual([r.points for r in d.reasons if r.code == 'RECENT_ACCOUNT_RECOVERY'], [17])

    def test_occurrence_and_knowledge_use_decision_time_independent_of_transaction(self):
        c = self.context(((BASE-timedelta(hours=1), BASE),))
        c = replace(c, pix=replace(c.pix, transaction_at=BASE-timedelta(days=3)))
        self.assertEqual(self.points(c), [25])

    def test_equivalent_offsets_and_dst_use_elapsed_24_hours(self):
        from zoneinfo import ZoneInfo
        now = datetime(2018, 11, 5, tzinfo=ZoneInfo('America/Sao_Paulo'))
        occurred = datetime(2018, 11, 3, 23, 30, tzinfo=ZoneInfo('America/Sao_Paulo'))
        p = replace(example_pix('current'), transaction_at=now, destination_opened_at=now-timedelta(days=100))
        sec = SecuritySnapshot(occurred, occurred, now+timedelta(days=1), recoveries=((occurred, occurred),))
        self.assertEqual(self.points(Context(p, now, security=sec)), [25])


class AuditRegressionTests(unittest.TestCase):
    def test_invalid_ip_raw_is_retained_for_hash_reproduction_only_in_private_audit(self):
        hashes, canonical = [], []
        for ip in ('not-an-ip-A', 'not-an-ip-B'):
            payload = deepcopy(OFFICIAL_PAYLOAD)
            payload['source']['ip'] = ip
            normalized = normalize_payload(payload)
            self.assertEqual(normalized.invalid_ip_raw, ip)
            from antifraud.adapter import to_pix
            pix = to_pix(normalized, 'demo')
            dto = snapshot(KEY, normalized, Context(pix, pix.transaction_at), Policy())
            audit = dto.audit
            envelope = {'payload': audit['payload'], 'invalid_ip_raw': audit['invalid_ip_raw']}
            encoded = json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
            self.assertEqual(sha256(encoded.encode()).hexdigest(), audit['payload_hash'])
            self.assertNotIn(ip, dto.public_json)
            hashes.append(normalized.payload_hash)
            canonical.append(normalized.canonical_payload)
        self.assertNotEqual(*hashes)
        self.assertEqual(*canonical)

    def test_valid_ip_audit_also_reproduces_hash(self):
        normalized = normalize_payload(deepcopy(OFFICIAL_PAYLOAD))
        self.assertIsNone(normalized.invalid_ip_raw)
        from antifraud.adapter import to_pix
        p = to_pix(normalized, 'demo')
        audit = snapshot(KEY, normalized, Context(p, p.transaction_at), Policy()).audit
        envelope = {'payload': audit['payload'], 'invalid_ip_raw': audit['invalid_ip_raw']}
        encoded = json.dumps(envelope, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)
        self.assertEqual(sha256(encoded.encode()).hexdigest(), audit['payload_hash'])

    def test_expired_available_evidence_is_stale_in_public_dto(self):
        normalized = normalize_payload(deepcopy(OFFICIAL_PAYLOAD))
        from antifraud.adapter import to_pix
        pix = to_pix(normalized, 'demo')
        item = replace(evidence(pix.payee_document), valid_until=pix.transaction_at)
        c = Context(pix, pix.transaction_at, dict_evidence=(('payee', item),), dict_queried=('payee',))
        dto = snapshot(KEY, normalized, c, Policy())
        self.assertEqual(dto.public['metadata']['enrichment']['payee']['state'], 'stale')
        self.assertIn('DICT_CHECK_UNAVAILABLE', dto.public['metadata']['reasons'])


class ContextConsistencyTests(unittest.TestCase):
    def test_explicit_query_action_requires_queried_role(self):
        with self.assertRaises(ValueError):
            Context(example_pix('current'), BASE, dict_actions=(('payee', 'queried'),))

    def test_queried_role_cannot_be_explicitly_skipped_or_cached(self):
        for action in ('skipped_confidence', 'skipped_after_initial', 'cache'):
            with self.subTest(action=action), self.assertRaises(ValueError):
                Context(example_pix('current'), BASE, dict_queried=('payer',),
                        dict_actions=(('payer', action),), dict_evidence=(('payer', evidence(A)),))

    def test_cache_requires_present_valid_evidence_for_exact_role(self):
        for items in ((), (('payer', replace(evidence(A), state='unavailable')),),
                      (('payer', replace(evidence(A), valid_until=BASE)),)):
            with self.subTest(items=items), self.assertRaises(ValueError):
                Context(example_pix('current'), BASE, dict_actions=(('payer', 'cache'),), dict_evidence=items)
        c = Context(example_pix('current'), BASE, dict_actions=(('payer', 'cache'),), dict_evidence=(('payer', evidence(A)),))
        self.assertNotIn('DICT_CHECK_UNAVAILABLE', {r.code for r in evaluate(c).reasons})

    def test_query_without_evidence_is_allowed_and_charged_when_actions_agree(self):
        c = Context(example_pix('current'), BASE, dict_actions=(('payer', 'queried'),), dict_queried=('payer',))
        self.assertEqual([r.points for r in evaluate(c).reasons if r.code == 'DICT_CHECK_UNAVAILABLE'], [10])


class RestrictionScopeTests(unittest.TestCase):
    def test_account_type_must_match_role_and_both_is_document_wide(self):
        pix = example_pix('current')
        for role, account in (('payee', pix.payer), ('payer', pix.destination),
                              ('both', pix.payer), ('both', pix.destination), ('payer', pix), ('payee', 1)):
            with self.subTest(role=role, account=account), self.assertRaises((TypeError, ValueError)):
                Restriction(A, role, BASE, BASE, BASE+timedelta(days=1), account=account)

    def test_valid_scoped_and_document_wide_restrictions_apply(self):
        pix = example_pix('current', payee=A)
        for role, account, expected_roles in (('payer', pix.payer, ('payer',)),
                                             ('payee', pix.destination, ('payee',)),
                                             ('both', None, ('payer', 'payee'))):
            with self.subTest(role=role):
                r = Restriction(A, role, BASE, BASE, BASE+timedelta(days=1), account=account)
                for candidate in ('payer', 'payee'):
                    self.assertEqual(r.applies(pix, candidate, BASE), candidate in expected_roles)

    def test_valid_account_of_same_type_but_different_subject_is_not_applicable(self):
        pix = example_pix('current')
        other = replace(pix.payer, tenant='other')
        r = Restriction(A, 'payer', BASE, BASE, BASE+timedelta(days=1), account=other)
        self.assertFalse(r.applies(pix, 'payer', BASE))


class RequiredOriginTests(unittest.TestCase):
    def test_device_must_be_explicit_and_nonempty(self):
        values = asdict(example_pix('current'))
        values['payer'] = example_pix('current').payer
        values['destination'] = example_pix('current').destination
        del values['device_id']
        with self.assertRaises(TypeError):
            Pix(**values)
        with self.assertRaises(ValueError):
            replace(example_pix('current'), device_id='')

    def test_reputation_provenance_must_be_explicit_and_nonblank(self):
        with self.assertRaises(TypeError):
            DictEvidence(A, 'available', BASE, BASE, BASE+timedelta(days=1))
        for provenance in ('', '  '):
            with self.subTest(provenance=provenance), self.assertRaises(ValueError):
                replace(evidence(A), provenance=provenance)


class RuntimeBoundaryTests(unittest.TestCase):
    def test_clock_validation_does_not_delegate_ranges_to_runtime_parser(self):
        # Emulate a runtime that normalizes invalid clock values instead of rejecting.
        for clock in ('24:00:00', '25:00:00', '23:60:00', '23:59:60'):
            for field in ('transaction_date', 'source_account', 'destination_account'):
                payload = deepcopy(OFFICIAL_PAYLOAD)
                invalid = f'2026-10-17T{clock}Z'
                if field == 'transaction_date':
                    payload[field] = invalid
                else:
                    payload[field]['opening_date'] = invalid
                with self.subTest(clock=clock, field=field):
                    with patch.object(contract, 'datetime', wraps=datetime) as parser:
                        parser.fromisoformat.return_value = datetime(2026, 10, 18, tzinfo=timezone.utc)
                        with self.assertRaises(ValidationError) as caught:
                            normalize_payload(payload)
                        self.assertEqual((caught.exception.http_status, caught.exception.code), (422, 'QIT002004'))

    def test_example_environment_starts_app_outside_container(self):
        env = {key: value for key, value in os.environ.items() if key not in {'PIX_POLICY_FILE'}}
        env.update({k: v for k, v in dotenv_values(ROOT / '.env.example').items() if v is not None})
        env['PYTHONPATH'] = str(ROOT / 'src')
        result = subprocess.run([sys.executable, '-c', 'import app'], cwd=ROOT / 'src',
                                env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


class DTOFormattingTests(unittest.TestCase):
    def test_dto_formats_supplied_decision_without_executing_engine(self):
        from antifraud.adapter import to_pix
        normalized = normalize_payload(deepcopy(OFFICIAL_PAYLOAD))
        p = to_pix(normalized, 'demo')
        c = Context(p, p.transaction_at)
        policy = Policy()
        decision = evaluate(c, policy)
        # Patch the criterion call too: it catches even a cached `from ... import evaluate` alias.
        with (patch('antifraud.engine.evaluate', side_effect=AssertionError('DTO must not run rules')),
              patch('antifraud.rules.Criterion.evaluate', side_effect=AssertionError('DTO must not run criteria'))):
            dto = PixTransactionDTO.from_decision(KEY, normalized, c, policy, decision)
        self.assertEqual(dto.public['status'], decision.status)
        self.assertEqual(dto.public['metadata']['score'], decision.score)
        self.assertEqual(dto.audit['decision'], json.loads(json.dumps(asdict(decision))))

    def test_dto_rejects_policy_version_mismatch(self):
        from antifraud.adapter import to_pix
        normalized = normalize_payload(deepcopy(OFFICIAL_PAYLOAD))
        p = to_pix(normalized, 'demo')
        c = Context(p, p.transaction_at)
        with self.assertRaises(ValueError):
            PixTransactionDTO.from_decision(KEY, normalized, c, Policy(), replace(evaluate(c), policy_version='different'))
