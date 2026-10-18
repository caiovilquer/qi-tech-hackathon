"""Coverage and scenario invariants for the 11-case, nine-factor Pix matrix."""

import unittest
from collections import defaultdict
from dataclasses import replace
from math import prod

from antifraud.context import DictEvidence
from antifraud.engine import evaluate
from tests.support.pix_permutation_matrix import (
    BANK_SIGNALS, CASE_NAMES, FACTOR_LEVELS, build_context, generate_rows, summarize,
)


class PixPermutationMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = list(generate_rows())
        cls.index = {
            (row["caso"], *(row[name] for name in FACTOR_LEVELS)): row
            for row in cls.rows
        }

    def row(self, case, **changes):
        factors = dict(origem="PF", destino="PF", horario="dia", valor="medio",
                       idade_origem="velha", idade_destino="velha",
                       tentativas="unica", recorrencia="primeira", localizacao="habitual")
        factors.update(changes)
        return self.index[(case, *(factors[name] for name in FACTOR_LEVELS))]

    def test_full_cartesian_product_and_applicability_are_explicit(self):
        per_case = prod(len(levels) for levels in FACTOR_LEVELS.values())
        self.assertEqual(per_case, 1152)
        self.assertEqual(len(self.rows), len(self.index))
        self.assertEqual(len(self.rows), len(CASE_NAMES) * per_case)
        result = summarize(self.rows)
        self.assertEqual(result["total_combinacoes"], 12672)
        self.assertEqual(result["total_avaliadas"], 10080)
        self.assertEqual(result["total_inaplicaveis"], 2592)
        self.assertEqual(result["totais_status"],
                         {"approved": 1572, "two_factor_required": 4080, "rejected": 4428})
        self.assertEqual({case: result["casos"][str(case)]["avaliadas"] for case in CASE_NAMES},
                         {1: 1152, 2: 1152, 3: 1152, 4: 1152, 5: 1152,
                          6: 1152, 7: 1152, 8: 1152, 9: 144, 10: 576, 11: 144})
        self.assertTrue(all(row["status"] in ("approved", "two_factor_required", "rejected")
                            if row["aplicabilidade"] == "avaliado"
                            else row["status"] == "" and row["motivo_inaplicavel"]
                            for row in self.rows))

    def test_location_has_no_effect_on_the_current_engine(self):
        grouped = defaultdict(set)
        for row in self.rows:
            key = (row["caso"], *(row[name] for name in FACTOR_LEVELS if name != "localizacao"))
            grouped[key].add((row["aplicabilidade"], row["status"], row["score"], row["motivos"]))
        self.assertEqual(len(grouped), 4224)
        self.assertTrue(all(len(outcomes) == 1 for outcomes in grouped.values()))

    def test_without_bank_signals_theft_coercion_and_old_device_auth_do_not_change_decision(self):
        for factors in self._factor_combinations():
            baseline = self.index[(5, *factors)]
            expected = (baseline["status"], baseline["score"], baseline["motivos"])
            for case in (3, 8):
                row = self.index[(case, *factors)]
                self.assertEqual((row["status"], row["score"], row["motivos"]), expected)
            for case in (1, 2):
                decision = evaluate(build_context(case, dict(zip(FACTOR_LEVELS, factors)), bank_signals=False))
                self.assertEqual((decision.status, decision.score), (baseline["status"], int(baseline["score"])))
        context = build_context(8, self._default_factors())
        self.assertEqual(context.security.device_state, "registered")
        self.assertEqual(len(context.auth_events), 1)
        self.assertEqual(context.auth_events[0].state, "passed")

    def test_reported_theft_and_duress_reject_every_combination(self):
        for case, code in ((1, "DEVICE_THEFT_REPORTED"), (2, "DURESS_REPORTED")):
            rows = [row for row in self.rows if row["caso"] == case]
            self.assertTrue(all(row["status"] == "rejected" and code in row["motivos"].split("|") for row in rows))
            baseline = {tuple(row[name] for name in FACTOR_LEVELS): row["score"]
                        for row in self.rows if row["caso"] == 5}
            # A floor, not points: the behavioural score is the same as without the report.
            self.assertTrue(all(row["score"] == baseline[tuple(row[name] for name in FACTOR_LEVELS)] for row in rows))

    def test_ceilings_turn_only_score_rejections_into_2fa(self):
        for case in (9, 10, 11):
            for row in self.rows:
                if row["caso"] != case or row["aplicabilidade"] != "avaliado":
                    continue
                capped = {"BANK_AUTHORIZED_CONTEXT", "SAME_HOLDER_TRANSFER"} & set(row["motivos"].split("|"))
                self.assertNotEqual(row["status"], "rejected")
                self.assertEqual(bool(capped), int(row["score"]) >= 70, row)
        approved = self.row(10, valor="baixo", idade_destino="velha")
        self.assertEqual(approved["status"], "approved")
        self.assertNotIn("SAME_HOLDER_TRANSFER", approved["motivos"])

    def test_ceilings_never_override_floors_or_adverse_reputation(self):
        factors = dict(self._default_factors(), origem="PJ", destino="PF", idade_origem="recente",
                       idade_destino="recente", tentativas="multiplas", valor="alto")
        authorized = build_context(11, factors)
        self.assertEqual(evaluate(authorized).status, "two_factor_required")
        revoked = replace(authorized, security=replace(authorized.security, device_state="revoked"))
        self.assertEqual(evaluate(revoked).status, "rejected")
        evidence = dict(authorized.dict_evidence)
        self.assertIsInstance(evidence["payee"], DictEvidence)
        adverse = replace(authorized, dict_evidence=(("payer", evidence["payer"]),
                                                     ("payee", replace(evidence["payee"], open_reports=1))))
        decision = evaluate(adverse)
        self.assertEqual(decision.status, "rejected")
        self.assertNotIn("BANK_AUTHORIZED_CONTEXT", {reason.code for reason in decision.reasons})
        self.assertEqual(set(BANK_SIGNALS), {1, 2, 9, 11})

    def test_suspicion_and_fan_in_are_visible_when_evidence_is_supplied(self):
        self.assertTrue(all("PAYEE_DICT_ADVERSE" in row["motivos"]
                            for row in self.rows if row["caso"] == 6))
        self.assertTrue(all("PAYER_DICT_ADVERSE" in row["motivos"]
                            for row in self.rows if row["caso"] == 7))
        young_high = self.row(4, valor="alto", idade_destino="recente")
        baseline = self.row(5, valor="alto", idade_destino="recente")
        self.assertIn("MULE_FAN_IN", young_high["motivos"])
        self.assertNotIn("MULE_FAN_IN", baseline["motivos"])

    def test_factor_levels_reach_the_intended_policy_features(self):
        self.assertFalse(self.row(5, idade_origem="recente")["perfil_geral"])
        self.assertTrue(self.row(5, idade_origem="velha")["perfil_geral"])
        self.assertFalse(self.row(5, recorrencia="primeira")["perfil_destinatario"])
        self.assertTrue(self.row(5, recorrencia="recorrente")["perfil_destinatario"])
        self.assertIn("RECENT_PAYEE_ACCOUNT", self.row(5, idade_destino="recente")["motivos"])
        self.assertNotIn("RECENT_PAYEE_ACCOUNT", self.row(5, idade_destino="velha")["motivos"])
        self.assertIn("AMOUNT_ABOVE_PROFILE", self.row(5, valor="alto")["motivos"])
        self.assertNotIn("AMOUNT_ABOVE_PROFILE", self.row(5, valor="baixo")["motivos"])
        for payer_kind in ("PF", "PJ"):
            self.assertIn("VELOCITY_BURST", self.row(5, origem=payer_kind, tentativas="multiplas")["motivos"])
            self.assertNotIn("VELOCITY_BURST", self.row(5, origem=payer_kind, tentativas="unica")["motivos"])

    def test_case_specific_constraints_do_not_fabricate_results(self):
        self.assertEqual(self.row(9, origem="PF", idade_origem="recente", tentativas="multiplas")["aplicabilidade"],
                         "inaplicavel")
        self.assertEqual(self.row(9, origem="PJ", idade_origem="recente", tentativas="multiplas")["aplicabilidade"],
                         "avaliado")
        self.assertEqual(self.row(10, origem="PF", destino="PJ")["aplicabilidade"], "inaplicavel")
        self.assertTrue(build_context(10, self._default_factors()).pix.same_holder)
        self.assertEqual(self.row(11, origem="PJ", destino="PF", tentativas="multiplas")["aplicabilidade"],
                         "avaliado")
        self.assertEqual(self.row(11, origem="PJ", destino="PJ", tentativas="multiplas")["aplicabilidade"],
                         "inaplicavel")

    def test_bank_revocation_changes_the_old_device_case(self):
        context = build_context(8, self._default_factors())
        revoked = replace(context, security=replace(context.security, device_state="revoked"))
        decision = evaluate(revoked)
        self.assertEqual(decision.status, "rejected")
        self.assertIn("DEVICE_REVOKED", {reason.code for reason in decision.reasons})

    @staticmethod
    def _default_factors():
        return dict(origem="PF", destino="PF", horario="dia", valor="medio",
                    idade_origem="velha", idade_destino="velha", tentativas="unica",
                    recorrencia="primeira", localizacao="habitual")

    @staticmethod
    def _factor_combinations():
        seen = set()
        for row in PixPermutationMatrixTests.rows:
            if row["caso"] != 5:
                continue
            key = tuple(row[name] for name in FACTOR_LEVELS)
            if key not in seen:
                seen.add(key)
                yield key


if __name__ == "__main__":
    unittest.main()
