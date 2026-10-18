"""Deterministic factorial scenarios for the pure Pix decision engine.

Run with ``PYTHONPATH=src:. python3 -m tests.support.pix_permutation_matrix``.
This deliberately exercises Context/Policy/evaluate, not HTTP, providers or SQL.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path
from zoneinfo import ZoneInfo

from antifraud.context import (
    APPROVED, TWO_FA, AccountKey, AuthEvent, Context, DestinationKey,
    DictEvidence, ExecutionEvent, HistoricalAnalysis, Pix, SecuritySnapshot,
)
from antifraud.engine import evaluate
from antifraud.policy import Policy


ROOT = Path(__file__).resolve().parents[2]
TZ = ZoneInfo("America/Sao_Paulo")
POLICY = Policy()
FACTOR_LEVELS = {
    "origem": ("PF", "PJ"),
    "destino": ("PF", "PJ"),
    "horario": ("dia", "noite"),
    "valor": ("baixo", "medio", "alto"),
    "idade_origem": ("recente", "velha"),
    "idade_destino": ("recente", "velha"),
    "tentativas": ("unica", "multiplas"),
    "recorrencia": ("primeira", "recorrente"),
    "localizacao": ("desconhecida", "habitual", "distante"),
}
CASE_NAMES = {
    1: "Aparelho roubado com aplicativo desbloqueado",
    2: "Usuário coagido a enviar Pix",
    3: "Usuário legítimo envia para contas diferentes",
    4: "Usuário legítimo recebe de contas diferentes",
    5: "Usuário faz Pix",
    6: "Envio para destinatário suspeito",
    7: "Recebimento de remetente suspeito",
    8: "Novo telefone e retorno ao aparelho antigo",
    9: "Empresa abre filial em outro país com alta movimentação",
    10: "Transferência para a mesma titularidade",
    11: "Empresa paga funcionários em lote",
}
# Explicit bank facts per case. The engine never infers theft, coercion or a
# business authorization from behaviour; these are premises of the scenario.
BANK_SIGNALS = {
    1: {"theft_reported": True},       # holder reported the phone from another channel
    2: {"duress_reported": True},      # bank duress signal, e.g. panic credential
    9: {"context_authorized": True},   # company declared the branch operation to the bank
    11: {"context_authorized": True},  # payroll batch registered with the bank
}
AMOUNTS = {"baixo": 5_000, "medio": 15_000, "alto": 120_000}
LOCATIONS = {
    "desconhecida": ("", ""),
    "habitual": ("-23.5505", "-46.6333"),
    "distante": ("-3.1190", "-60.0217"),
}
CSV_FIELDS = (
    "caso", "descricao", *FACTOR_LEVELS, "latitude", "longitude", "aplicabilidade",
    "motivo_inaplicavel", "status", "score", "confidence_pre", "motivos",
    "perfil_geral", "perfil_destinatario", "estado_dispositivo", "politica",
)


def cpf_fixture(seed: int) -> str:
    digits = [int(char) for char in f"{100_000_000 + seed:09d}"]
    for length in (9, 10):
        remainder = sum(value * weight for value, weight in zip(digits, range(length + 1, 1, -1))) % 11
        digits.append(0 if remainder < 2 else 11 - remainder)
    return "".join(map(str, digits))


def cnpj_fixture(seed: int) -> str:
    digits = [int(char) for char in f"{200_000_000_000 + seed:012d}"]
    for weights in ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
                    (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)):
        remainder = sum(value * weight for value, weight in zip(digits, weights)) % 11
        digits.append(0 if remainder < 2 else 11 - remainder)
    return "".join(map(str, digits))


def combinations():
    for values in product(*FACTOR_LEVELS.values()):
        yield dict(zip(FACTOR_LEVELS, values))


def inapplicable(case: int, factors: dict[str, str]) -> str:
    if case == 9:
        if factors["origem"] != "PJ":
            return "filial de empresa exige origem PJ"
        if factors["idade_origem"] != "recente":
            return "nova conta exige origem recente"
        if factors["tentativas"] != "multiplas":
            return "alta movimentação exige múltiplas tentativas"
    if case == 10 and factors["origem"] != factors["destino"]:
        return "mesma titularidade exige naturezas iguais"
    if case == 11:
        if factors["origem"] != "PJ" or factors["destino"] != "PF":
            return "folha de funcionários exige PJ para PF"
        if factors["tentativas"] != "multiplas":
            return "pagamento em lote exige múltiplas tentativas"
    return ""


def build_context(case: int, factors: dict[str, str], *, bank_signals: bool = True) -> Context:
    """Build only facts visible to the engine. Geography is not a field.

    Theft, coercion and authorizations reach the engine only as the explicit bank
    facts in ``BANK_SIGNALS``; ``bank_signals=False`` removes them.
    """
    hour = 14 if factors["horario"] == "dia" else 22
    at = datetime(2026, 10, 18, hour, 0, tzinfo=TZ)
    payer_doc = "12345678909" if factors["origem"] == "PF" else "11222333000181"
    payee_doc = (payer_doc if case == 10 else
                 "98765432100" if factors["destino"] == "PF" else cnpj_fixture(1))
    payer = AccountKey("demo", "0001", "10001", payer_doc)
    destination = DestinationKey("60701190", "3675", "20001")
    destination_opened = at - timedelta(days=2 if factors["idade_destino"] == "recente" else 180)
    current = Pix("current", payer, payee_doc, destination, AMOUNTS[factors["valor"]],
                  at, destination_opened, "dev-old")
    history: list[HistoricalAnalysis] = []
    executions: list[ExecutionEvent] = []
    auth: list[AuthEvent] = []

    def add_previous(identifier: str, previous_payer: AccountKey, previous_payee: str,
                     previous_destination: DestinationKey, when: datetime, *,
                     executed: bool = False, amount: int = 15_000,
                     device: str = "dev-old", status: str = APPROVED,
                     challenge: str | None = None) -> None:
        previous = Pix(identifier, previous_payer, previous_payee, previous_destination,
                       amount, when, at - timedelta(days=365), device)
        night_qualifies = (when.astimezone(TZ).hour >= POLICY.night_start_hour
                           or when.astimezone(TZ).hour < POLICY.night_end_hour)
        night_qualifies &= previous.payer_pf and previous.payee_pf and not previous.same_holder
        history.append(HistoricalAnalysis(previous, when + timedelta(seconds=1), len(history) + 1,
                                          status, challenge, "registered", night_qualifies))
        if executed:
            executions.append(ExecutionEvent(f"exec-{identifier}", previous_payer, identifier,
                                             "executed", when, when + timedelta(seconds=1)))

    # An old destination also has six confirmed receipts from other payers.
    # They are older than the 24-hour fan-in window and do not imply that this
    # payer has transferred to the destination before.
    if factors["idade_destino"] == "velha":
        for index, days in enumerate((60, 55, 50, 45, 40, 35)):
            document = (cpf_fixture(800 + index) if factors["origem"] == "PF"
                        else cnpj_fixture(800 + index))
            other_payer = AccountKey("demo", "0001", f"{70_000 + index}", document)
            add_previous(f"old-incoming-{index}", other_payer, payee_doc, destination,
                         at - timedelta(days=days), executed=True)

    # Recent source: no broad payment profile. Old source: six confirmed payments
    # over 30 days, sufficient for the five-payment/three-day general profile.
    if factors["idade_origem"] == "velha":
        for index, days in enumerate((30, 25, 20, 15, 10, 5)):
            varied = case in (3, 11)
            other_payee = cpf_fixture(200 + index) if varied else "52998224725"
            other_destination = DestinationKey("60701190", "3675",
                                               f"{30_000 + index if varied else 30_000}")
            add_previous(f"profile-{index}", payer, other_payee, other_destination,
                         at - timedelta(days=days), executed=True)

    # Recurrence is independent of destination opening age: two confirmed,
    # mature payments to this same destination, 36 and 30 hours earlier.
    if factors["recorrencia"] == "recorrente":
        for index, hours in enumerate((36, 30)):
            add_previous(f"recurring-{index}", payer, payee_doc, destination,
                         at - timedelta(hours=hours), executed=True)

    # "Multiple" reaches the policy's soft velocity threshold for the payer
    # kind: four earlier attempts for PF, twenty-four for PJ, all within 10 min.
    if factors["tentativas"] == "multiplas":
        soft_limit = POLICY.velocity_pf[0] if factors["origem"] == "PF" else POLICY.velocity_pj[0]
        for index in range(soft_limit - 1):
            varied = case in (3, 11)
            other_payee = cpf_fixture(400 + index) if varied else "52998224725"
            other_destination = DestinationKey("60701190", "3675",
                                               f"{40_000 + index if varied else 40_000}")
            add_previous(f"attempt-{index}", payer, other_payee, other_destination,
                         at - timedelta(seconds=20 * (index + 1)),
                         executed=case in (3, 9, 11))

    if case == 4:
        # Four distinct earlier payers to the current destination. The current
        # payer is the fifth, which can trigger MULE_FAN_IN for a young account.
        for index in range(4):
            document = (cpf_fixture(600 + index) if factors["origem"] == "PF"
                        else cnpj_fixture(600 + index))
            other_payer = AccountKey("demo", "0001", f"{50_000 + index}", document)
            add_previous(f"incoming-{index}", other_payer, payee_doc, destination,
                         at - timedelta(minutes=15 * (index + 1)), executed=True)

    if case == 8:
        # A successful authentication on the new phone is not a device
        # registration or a revocation of the old one.
        when = at - timedelta(days=2)
        add_previous("new-phone", payer, "52998224725",
                     DestinationKey("60701190", "3675", "60001"), when,
                     device="dev-new", status=TWO_FA, challenge="challenge-new",
                     executed=True)
        auth.append(AuthEvent("auth-new", payer, "new-phone", "challenge-new",
                              "passed", True, when + timedelta(seconds=2),
                              when + timedelta(seconds=2)))

    security = SecuritySnapshot(at - timedelta(hours=1), at - timedelta(hours=1),
                                at + timedelta(days=1), device_state="registered",
                                **(BANK_SIGNALS.get(case, {}) if bank_signals else {}))
    evidence = []
    for role, document in (("payer", payer_doc), ("payee", payee_doc)):
        adverse = case == (7 if role == "payer" else 6)
        evidence.append((role, DictEvidence(document, "available", at - timedelta(hours=1),
                                            at - timedelta(hours=1), at + timedelta(days=1),
                                            open_reports=int(adverse), provenance="synthetic-cache")))
    return Context(current, at, tuple(history), tuple(executions), tuple(auth), security,
                   tuple(evidence), dict_actions=(("payer", "cache"), ("payee", "cache")))


def generate_rows():
    for case, description in CASE_NAMES.items():
        for factors in combinations():
            latitude, longitude = LOCATIONS[factors["localizacao"]]
            row = {"caso": case, "descricao": description, **factors,
                   "latitude": latitude, "longitude": longitude}
            reason = inapplicable(case, factors)
            if reason:
                row.update(aplicabilidade="inaplicavel", motivo_inaplicavel=reason,
                           status="", score="", confidence_pre="", motivos="",
                           perfil_geral="", perfil_destinatario="", estado_dispositivo="", politica=POLICY.version)
            else:
                decision = evaluate(build_context(case, factors), POLICY)
                row.update(aplicabilidade="avaliado", motivo_inaplicavel="",
                           status=decision.status, score=decision.score,
                           confidence_pre=decision.c_pre,
                           motivos="|".join(reason.code for reason in decision.reasons),
                           perfil_geral=decision.general_profile,
                           perfil_destinatario=decision.payee_profile,
                           estado_dispositivo=decision.device_state, politica=decision.policy_version)
            yield row


def summarize(rows):
    summary = {}
    for case, description in CASE_NAMES.items():
        subset = [row for row in rows if row["caso"] == case]
        counts = Counter(row["status"] if row["aplicabilidade"] == "avaliado" else "inaplicavel"
                         for row in subset)
        summary[str(case)] = {"descricao": description, "combinacoes": len(subset),
                              "avaliadas": len(subset) - counts["inaplicavel"],
                              "inaplicaveis": counts["inaplicavel"],
                              "approved": counts["approved"],
                              "two_factor_required": counts["two_factor_required"],
                              "rejected": counts["rejected"]}
    totals = {status: sum(item[status] for item in summary.values())
              for status in ("approved", "two_factor_required", "rejected")}
    return {"politica": POLICY.version, "fingerprint": POLICY.fingerprint,
            "total_combinacoes": len(rows), "total_avaliadas": sum(item["avaliadas"] for item in summary.values()),
            "total_inaplicaveis": sum(item["inaplicaveis"] for item in summary.values()),
            "totais_status": totals,
            "casos": summary}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    configured = json.loads((ROOT / "src/antifraud/policies.json").read_text(encoding="utf-8"))
    if configured["policies"][POLICY.version] != POLICY.configuration():
        raise RuntimeError("Default Policy diverges from the configured runtime policy")
    rows = list(generate_rows())
    summary = summarize(rows)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "pix_permutation_matrix.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (args.output_dir / "pix_permutation_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
