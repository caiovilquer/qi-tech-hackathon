"""Independent policy invariants: expected facts are hand-specified, not golden output.

PolicyTests ported from docs/referencia/test_politica_pix.py (36 tests).
The 20 sequential OrchestrationTests remain reference-only (docs/referencia/test_politica_pix.py).
No test here claims database concurrency, HTTP integration or measured efficacy.
"""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import importlib.util
import unittest

from tests.support.pix_scenarios import (
    A, B, C, EMPRESA, BASE, OLD, Analysis, FixtureProviders, Simulator, cents,
    cpf_fixture, example_pix, seed_executed, seed_profile,
)
from antifraud.context import (
    APPROVED, TWO_FA, REJECTED, AccountKey, AuthEvent, Context, DictEvidence,
    ExecutionEvent, HistoricalAnalysis, Restriction, SecuritySnapshot,
)
from antifraud.policy import Policy
from antifraud.features import effective, exposure
from antifraud.engine import evaluate



def codes(decision):
    return {reason.code for reason in decision.reasons}


def security(pix, state="registered", trusted=False, **kwargs):
    return SecuritySnapshot(BASE-timedelta(days=100), BASE-timedelta(days=100),
                            BASE+timedelta(days=100), state, trusted_beneficiary=trusted, **kwargs)


def add_security(sim, pix, state="registered", trusted=False, **kwargs):
    sim.providers.security_fixtures[(pix.payer, pix.device_id, pix.destination)] = [security(pix, state, trusted, **kwargs)]


def evidence(document, **kwargs):
    return DictEvidence(document, "available", BASE-timedelta(minutes=1), BASE-timedelta(minutes=1),
                        BASE+timedelta(minutes=4), **kwargs, provenance="synthetic_fixture")


def history(pix, status=APPROVED, challenge=None, device="registered", qualifies=False, decision_at=None):
    return HistoricalAnalysis(pix, decision_at or pix.transaction_at, 1, status, challenge, device, qualifies)


def execution(pix, state="executed", occurred=None, available=None):
    return ExecutionEvent("exec:"+pix.id, pix.payer, pix.id, state,
                          occurred or pix.transaction_at, available or pix.transaction_at)


class PolicyTests(unittest.TestCase):
    def test_approved_and_auth_passed_are_not_execution(self):
        prior = example_pix("prior", 10_000, BASE-timedelta(days=2))
        item = history(prior, TWO_FA, "challenge-1")
        auth = AuthEvent("a1", prior.payer, prior.id, "challenge-1", "passed", True,
                         prior.transaction_at, prior.transaction_at)
        c = Context(example_pix("current", 100_000), BASE, (item,), auth_events=(auth,))
        self.assertFalse(effective(item, c))
        self.assertTrue(exposure(item, c))
        self.assertFalse(evaluate(c).recipient_established)
        self.assertFalse(effective(replace(item, status=APPROVED), c))
        self.assertTrue(effective(item, replace(c, execution_events=(execution(prior),))))

    def test_execution_known_after_analysis_does_not_leak(self):
        sim = Simulator()
        for i in range(2):
            p = example_pix(f"old-{i}", 10_000, BASE-timedelta(days=i+2))
            sim.history.append(history(p))
            sim.record_execution(execution(p, available=BASE+timedelta(seconds=1)))
        result = sim.analyze(example_pix("current", 100_000), BASE)
        self.assertFalse(result.decision.payee_profile)
        self.assertFalse(result.decision.recipient_established)
        later = sim.analyze(example_pix("later", 100_000, BASE+timedelta(seconds=2)), BASE+timedelta(seconds=2))
        self.assertTrue(later.decision.payee_profile)
        self.assertFalse(result.decision.payee_profile)  # old immutable result

    def test_future_execution_occurrence_does_not_leak_even_if_received(self):
        prior = example_pix("old", t=BASE-timedelta(days=3))
        c = Context(example_pix("new"), BASE, (history(prior),),
                    (execution(prior, occurred=BASE+timedelta(hours=1), available=BASE),))
        self.assertFalse(effective(c.history[0], c))

    def test_two_factor_failure_releases_only_linked_terminal_known_challenge(self):
        prior = example_pix("prior", 100_000, BASE-timedelta(minutes=1))
        item = history(prior, TWO_FA, "right")
        c = Context(example_pix("current"), BASE, (item,))
        auth = AuthEvent("a", prior.payer, prior.id, "wrong", "failed", True, BASE, BASE)
        self.assertTrue(exposure(item, replace(c, auth_events=(auth,))))
        self.assertTrue(exposure(item, replace(c, auth_events=(replace(auth, challenge_id="right", terminal=False),))))
        self.assertTrue(exposure(item, replace(c, auth_events=(replace(auth, challenge_id="right", available_at=BASE+timedelta(seconds=1)),))))
        self.assertFalse(exposure(item, replace(c, auth_events=(replace(auth, challenge_id="right"),))))
        self.assertTrue(exposure(item, replace(c, execution_events=(execution(prior),),
                                              auth_events=(replace(auth, challenge_id="right"),))))

    def test_failed_cancelled_execution_releases_exposure_not_intentions(self):
        prior = example_pix("prior", 300_000, BASE-timedelta(minutes=1))
        for state in ("failed", "cancelled"):
            c = Context(example_pix("current", 300_000), BASE, (history(prior),), (execution(prior, state),))
            self.assertFalse(exposure(c.history[0], c))
            self.assertIn("DAILY_AMOUNT", codes(evaluate(c)))

    def test_ten_distinct_ids_same_amount_same_destination_count(self):
        sim = Simulator()
        results = [sim.analyze(example_pix(str(i), t=BASE+timedelta(seconds=i)), BASE+timedelta(seconds=i)).decision
                   for i in range(10)]
        self.assertEqual(results[3].status, APPROVED)
        self.assertEqual(results[4].status, TWO_FA)
        self.assertEqual(results[9].status, REJECTED)
        self.assertIn("VELOCITY_BURST", codes(results[9]))
        self.assertEqual(dict(results[9].families)["Velocity"], 30)

    def test_receipt_velocity_detects_spread_event_dates(self):
        prior = tuple(history(example_pix(str(i), t=BASE-timedelta(days=i+1)), REJECTED,
                              decision_at=BASE-timedelta(seconds=i+1)) for i in range(9))
        d = evaluate(Context(example_pix("now"), BASE, prior))
        self.assertEqual(d.status, REJECTED)
        self.assertIn("RECEIPT_VELOCITY", codes(d))
        self.assertNotIn("VELOCITY_BURST", codes(d))
        self.assertEqual(dict(d.families)["Velocity"], 30)

    def test_window_is_open_left_closed_right_and_same_timestamp_counts(self):
        prior = tuple(history(example_pix(str(i), t=BASE)) for i in range(4))
        self.assertIn("VELOCITY_BURST", codes(evaluate(Context(example_pix("now"), BASE, prior))))
        boundary = tuple(replace(h, pix=replace(h.pix, transaction_at=BASE-timedelta(minutes=10)),
                                 decision_at=BASE-timedelta(minutes=10)) for h in prior)
        self.assertNotIn("VELOCITY_BURST", codes(evaluate(Context(example_pix("now"), BASE, boundary))))
        current = history(example_pix("now"))
        self.assertNotIn("VELOCITY_BURST", codes(evaluate(Context(example_pix("now"), BASE, (current,)*9))))

    def test_failed_two_factor_does_not_register_or_familiarize_device(self):
        sim = Simulator()
        seed_executed(sim, example_pix("older", t=BASE-timedelta(days=2), device="old-device"))
        first = sim.analyze(example_pix("new-1", 25_000, device="new-device"), BASE)
        self.assertEqual(first.decision.status, TWO_FA)
        sim.record_auth(AuthEvent("failed", first.context.pix.payer, "new-1", first.challenge_id,
                                  "failed", True, BASE, BASE))
        second = sim.analyze(example_pix("new-2", 25_000, BASE+timedelta(minutes=1), device="new-device"), BASE+timedelta(minutes=1))
        self.assertEqual(second.decision.status, TWO_FA)
        self.assertEqual(second.decision.device_state, "unknown")
        self.assertIn("DEVICE_CHANGE", codes(second.decision))

    def test_rejected_attempt_does_not_register_device(self):
        sim = Simulator()
        sim.history.append(history(example_pix("old-device", t=BASE-timedelta(days=1), device="old")))
        sim.history.append(history(example_pix("bad", t=BASE-timedelta(minutes=1), device="new"), REJECTED))
        decision = sim.analyze(example_pix("next", 25_000, device="new"), BASE).decision
        self.assertEqual(decision.status, TWO_FA)
        self.assertIn("DEVICE_CHANGE", codes(decision))

    def test_history_older_than_ninety_days_never_builds_profile(self):
        sim = Simulator()
        for i in range(6):
            seed_executed(sim, example_pix(f"old-{i}", 10_000, BASE-timedelta(days=90+i)))
        d = sim.analyze(example_pix("new", 100_000), BASE).decision
        self.assertFalse(d.general_profile)
        self.assertFalse(d.payee_profile)
        self.assertNotIn("AMOUNT_ABOVE_PROFILE", codes(d))

    def test_payee_two_execution_profile_independent_of_general(self):
        sim = Simulator()
        for i in range(2):
            seed_executed(sim, example_pix(f"old-{i}", 10_000, BASE-timedelta(days=i+2)))
        d = sim.analyze(example_pix("new", 100_000), BASE).decision
        self.assertFalse(d.general_profile)
        self.assertTrue(d.payee_profile)
        self.assertIn("AMOUNT_ABOVE_PROFILE", codes(d))
        self.assertNotIn("LOW_HISTORY_EXPOSURE", codes(d))
        self.assertEqual(dict(d.families)["Perfil"], 30)

    def test_profile_uses_median_not_exceptional_maximum(self):
        sim = Simulator()
        for i, amount in enumerate((10_000, 10_000, 1_000_000)):
            seed_executed(sim, example_pix(f"old-{i}", amount, BASE-timedelta(days=i+2)))
        d = sim.analyze(example_pix("new", 60_000), BASE).decision
        self.assertEqual(dict(d.families)["Perfil"], 30)

    def test_profile_maturity_requires_execution_to_be_twentyfour_hours_old(self):
        sim = Simulator()
        for i in range(2):
            p = example_pix(f"old-{i}", 10_000, BASE-timedelta(days=i+2))
            sim.history.append(history(p))
            sim.record_execution(execution(p, occurred=BASE-timedelta(hours=23), available=BASE-timedelta(hours=23)))
        self.assertFalse(sim.analyze(example_pix("new", 100_000), BASE).decision.payee_profile)

    def test_payee_profile_is_by_account_not_document(self):
        sim = Simulator(); seed_profile(sim)
        d = sim.analyze(example_pix("new", account="other-account"), BASE).decision
        self.assertTrue(d.general_profile)
        self.assertFalse(d.payee_profile)
        self.assertIn("NEW_PAYEE", codes(d))

    def test_payer_history_does_not_cross_account_tenant_branch(self):
        sim = Simulator(); seed_profile(sim)
        p = example_pix("new", source="other-source")
        self.assertFalse(sim.analyze(p, BASE).decision.general_profile)
        for changed in (replace(p.payer, tenant="other"), replace(p.payer, branch="other")):
            self.assertFalse(evaluate(Context(replace(p, payer=changed), BASE, tuple(sim.history), tuple(sim.executions))).general_profile)

    def test_default_unknown_is_not_clean_but_small_payment_can_approve(self):
        sim = Simulator()
        a = sim.analyze(example_pix("small"), BASE)
        self.assertEqual(a.decision.status, APPROVED)
        self.assertEqual(a.decision.score, 10)
        self.assertEqual(a.decision.c_pre, 60)
        self.assertEqual([e.state for _, e in a.context.dict_evidence], ["not_covered", "not_covered"])
        self.assertEqual(a.context.dict_queried, ("payer", "payee"))

    def test_valid_zero_is_distinct_from_unavailable_and_stale(self):
        sim = Simulator()
        sim.providers.dict_fixtures = {d: [evidence(d)] for d in (A, B)}
        clean = sim.analyze(example_pix("clean"), BASE)
        self.assertEqual(clean.decision.score, 0)
        sim2 = Simulator(); sim2.providers.down = True
        self.assertEqual(sim2.analyze(example_pix("down"), BASE).decision.score, 10)
        sim3 = Simulator()
        sim3.providers.dict_fixtures[B] = [replace(evidence(B), valid_until=BASE)]
        self.assertIn("DICT_CHECK_UNAVAILABLE", codes(sim3.analyze(example_pix("stale"), BASE).decision))

    def test_adverse_counters_do_not_imply_explicit_restriction(self):
        sim = Simulator()
        sim.providers.dict_fixtures = {A: [evidence(A)], B: [evidence(B, fraud_count=2, open_reports=2)]}
        d = sim.analyze(example_pix("adverse"), BASE).decision
        self.assertEqual(d.status, TWO_FA)
        self.assertEqual(d.score, 30)
        self.assertNotIn("PAYEE_DICT_RESTRICTED", codes(d))

    def test_same_document_adverse_evidence_is_not_double_counted(self):
        sim = Simulator()
        sim.providers.dict_fixtures[A] = [evidence(A, fraud_count=1)]
        decision = sim.analyze(example_pix("own-adverse", payee=A), BASE).decision
        self.assertEqual(decision.score, 30)
        self.assertEqual(decision.status, TWO_FA)
        self.assertEqual(codes(decision), {"PAYER_DICT_ADVERSE", "PAYEE_DICT_ADVERSE"})
        self.assertEqual(len(sim.providers.calls), 1)  # the second role reuses the same evidence

    def test_restriction_hard_override_both_roles_and_scope(self):
        for role, document in (("payer", A), ("payee", B)):
            with self.subTest(role=role):
                sim = Simulator(); p = example_pix("restricted")
                r = Restriction(document, role, BASE-timedelta(hours=1), BASE-timedelta(hours=1), BASE+timedelta(hours=1))
                sim.providers.dict_fixtures[document] = [evidence(document, restrictions=(r,))]
                self.assertEqual(sim.analyze(p, BASE).decision.status, REJECTED)
                scoped_account = p.payer if role == "payer" else p.destination
                wrong = replace(r, account=replace(scoped_account, account="different"))
                self.assertFalse(wrong.applies(p, role, BASE))

    def test_security_revocation_uses_decision_clock_and_no_dict_gate_override(self):
        sim = Simulator(); seed_profile(sim)
        p = example_pix("delayed", t=BASE-timedelta(hours=1))
        revoked = SecuritySnapshot(BASE, BASE, BASE+timedelta(hours=1), "revoked")
        sim.providers.security_fixtures[(p.payer, p.device_id, p.destination)] = [revoked]
        a = sim.analyze(p, BASE)
        self.assertEqual(a.decision.status, REJECTED)
        self.assertIn("DEVICE_REVOKED", codes(a.decision))

    def test_bank_restriction_still_applies_when_confidence_skips_dict(self):
        sim = Simulator(); seed_profile(sim)
        p = example_pix("known")
        r = Restriction(B, "payee", BASE, BASE, BASE+timedelta(hours=1))
        add_security(sim, p, restrictions=(r,))
        a = sim.analyze(p, BASE)
        self.assertEqual(a.decision.c_pre, 100)
        self.assertEqual(dict(a.context.dict_actions)["payee"], "skipped_confidence")
        self.assertEqual(a.decision.status, REJECTED)

    def test_self_transfer_does_not_halve_independent_risk(self):
        sim = Simulator()
        for i in range(2):
            seed_executed(sim, example_pix(f"old-{i}", 10_000, BASE-timedelta(days=i+2), payee=A))
        d = sim.analyze(example_pix("own", 100_000, payee=A), BASE).decision
        self.assertEqual(dict(d.families)["Perfil"], 30)
        self.assertNotIn("NEW_PAYEE", codes(d))

    def test_mule_fan_in_counts_observations_and_destination_account(self):
        current = example_pix("current", 50_000, opened=BASE-timedelta(days=10))
        incoming = tuple(history(example_pix(f"prior-{i}", 50_000, BASE-timedelta(minutes=i+1),
                        payer=cpf_fixture(i)), REJECTED) for i in range(4))
        d = evaluate(Context(current, BASE, incoming))
        self.assertIn("MULE_FAN_IN", codes(d))
        changed = replace(current, destination=replace(current.destination, participant="87654321"))
        self.assertNotIn("MULE_FAN_IN", codes(evaluate(Context(changed, BASE, incoming))))

    def test_fan_in_is_isolated_by_tenant(self):
        p = example_pix("current", 50_000, opened=BASE-timedelta(days=10))
        others = tuple(history(replace(example_pix(f"prior-{i}", 50_000, BASE-timedelta(minutes=i+1),
                        payer=cpf_fixture(i)), payer=AccountKey("other", "0001", str(i), cpf_fixture(i))), REJECTED)
                       for i in range(4))
        self.assertNotIn("MULE_FAN_IN", codes(evaluate(Context(p, BASE, others))))
        same_tenant = tuple(replace(h, pix=replace(h.pix, payer=replace(h.pix.payer, tenant="demo"))) for h in others)
        self.assertIn("MULE_FAN_IN", codes(evaluate(Context(p, BASE, same_tenant))))

    def test_supported_time_bounds_are_explicit_not_overflows(self):
        for year in (1, 9999):
            edge = datetime(year, 1, 1, tzinfo=timezone.utc)
            with self.assertRaisesRegex(ValueError, "UTC year"):
                example_pix("bad", t=edge)
            with self.assertRaisesRegex(ValueError, "UTC year"):
                evaluate(Context(example_pix("good"), edge))
            with self.assertRaisesRegex(ValueError, "UTC year"):
                Simulator().analyze(example_pix("good"), edge)
        for year in (2, 9998):
            edge = datetime(year, 1, 1, tzinfo=timezone.utc)
            pix = example_pix("boundary", t=edge, opened=edge)
            self.assertIsInstance(Simulator().analyze(pix, edge), Analysis)

    def test_daily_amount_counts_rejected_attempts(self):
        old = example_pix("prior", 300_000, BASE-timedelta(hours=2))
        d = evaluate(Context(example_pix("current", 300_000), BASE, (history(old, REJECTED),)))
        self.assertIn("DAILY_AMOUNT", codes(d))

    def test_structuring_uses_exact_inclusive_integer_bands(self):
        for amount, expected in ((84_999, False), (85_000, True), (100_000, True), (100_001, False)):
            with self.subTest(amount=amount):
                prior = tuple(history(example_pix(str(i), amount, BASE-timedelta(hours=i+1))) for i in range(2))
                d = evaluate(Context(example_pix("new", amount), BASE, prior))
                self.assertEqual("STRUCTURING" in codes(d), expected)

    def test_registered_device_and_unknown_first_are_not_unregistered(self):
        p = example_pix("new", 25_000)
        self.assertNotIn("DEVICE_POLICY_LIMIT", codes(evaluate(Context(p, BASE))))
        self.assertNotIn("DEVICE_POLICY_LIMIT", codes(evaluate(Context(p, BASE, security=security(p)))))
        self.assertEqual(evaluate(Context(p, BASE, security=security(p, "unregistered"))).status, TWO_FA)

    def test_unregistered_device_sum_uses_calendar_day(self):
        p = example_pix("new", 20_000, BASE.replace(hour=0, minute=10))
        earlier = tuple(history(example_pix(str(i), 20_000, p.transaction_at-timedelta(minutes=30+i)),
                                device="unregistered") for i in range(5))
        d = evaluate(Context(p, p.transaction_at, earlier, security=security(p, "unregistered")))
        self.assertNotIn("DEVICE_POLICY_LIMIT", codes(d))
        same_day = tuple(replace(h, pix=replace(h.pix, transaction_at=p.transaction_at-timedelta(minutes=1))) for h in earlier)
        self.assertIn("DEVICE_POLICY_LIMIT", codes(evaluate(Context(p, p.transaction_at, same_day,
                                                                  security=security(p, "unregistered")))))

    def test_night_crosses_midnight_and_strict_amount_borders(self):
        start = BASE.replace(hour=23, minute=50)
        first = example_pix("first", 90_000, start)
        p = example_pix("next", 90_000, start+timedelta(minutes=20))
        d = evaluate(Context(p, p.transaction_at, (history(first, qualifies=True),)))
        self.assertIn("NIGHT_SOFT_LIMIT", codes(d))
        for amount, soft, hard in ((100_000, False, False), (100_001, True, False),
                                   (300_000, True, False), (300_001, True, True)):
            p = example_pix("boundary", amount, start)
            found = codes(evaluate(Context(p, start)))
            self.assertEqual("NIGHT_SOFT_LIMIT" in found, soft)
            self.assertEqual("NIGHT_HARD_LIMIT" in found, hard)
        for hour, expected in ((19, False), (20, True), (5, True), (6, False)):
            p = example_pix("hour", 300_001, BASE.replace(hour=hour))
            self.assertEqual("NIGHT_HARD_LIMIT" in codes(evaluate(Context(p, p.transaction_at))), expected)

    def test_previous_execution_is_not_trusted_beneficiary_designation(self):
        sim = Simulator(); seed_profile(sim)
        p = example_pix("night", 400_000, BASE.replace(hour=23))
        self.assertIn("NIGHT_HARD_LIMIT", codes(sim.analyze(p, p.transaction_at).decision))
        d = evaluate(Context(p, p.transaction_at, security=security(p, trusted=True)))
        self.assertNotIn("NIGHT_HARD_LIMIT", codes(d))
        self.assertIn("NIGHT_SOFT_LIMIT", codes(d))

    def test_bank_limit_is_independent_of_trusted_beneficiary(self):
        p = example_pix("bank", 5_000)
        d = evaluate(Context(p, BASE, security=security(p, trusted=True, bank_limit_exceeded=True)))
        self.assertEqual(d.status, REJECTED)
        self.assertIn("BANK_EFFECTIVE_LIMIT", codes(d))

    def test_exact_money_and_valid_cpf_fixtures(self):
        self.assertEqual(cents("0.29"), 29)
        self.assertEqual(cents("1.15"), 115)
        with self.assertRaises(TypeError): cents(0.29)
        with self.assertRaises(ValueError): cents("1.151")
        for i in range(20):
            cpf = cpf_fixture(i)
            self.assertEqual(len(cpf), 11)
            for n in (9, 10):
                expected = (sum(int(cpf[j])*(n+1-j) for j in range(n))*10) % 11
                self.assertEqual(int(cpf[n]), 0 if expected == 10 else expected)

    def test_established_twenty_salary_payroll_and_pj_burst_not_immune(self):
        sim = Simulator()
        for i in range(20):
            for j in range(2):
                seed_executed(sim, example_pix(f"old-{i}-{j}", 250_000, BASE-timedelta(days=30+j*30+i%3),
                                               payer=EMPRESA, payee=cpf_fixture(i)))
        results = [sim.analyze(example_pix(f"salary-{i}", 250_000, BASE+timedelta(seconds=i*30),
                    payer=EMPRESA, payee=cpf_fixture(i)), BASE+timedelta(seconds=i*30)).decision for i in range(20)]
        self.assertEqual([d.status for d in results], [APPROVED]*20)
        p = example_pix("pj-current", payer=EMPRESA)
        prior = tuple(history(example_pix(f"pj-{i}", payer=EMPRESA), REJECTED) for i in range(99))
        self.assertEqual(evaluate(Context(p, BASE, prior)).status, REJECTED)
