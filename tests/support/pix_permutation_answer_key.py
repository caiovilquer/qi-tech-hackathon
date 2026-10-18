"""Compare the Pix matrix with an explicit, scenario-level answer key.

The answer key uses the case description and factor values only. It never uses
the engine's score or reasons to decide which status is acceptable.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from itertools import product
from pathlib import Path

from tests.support.pix_permutation_matrix import CASE_NAMES, FACTOR_LEVELS, ROOT


STATUS = ("approved", "two_factor_required", "rejected")
ANSWER_KEY = {
    1: ("dano_confirmado", ("rejected",),
        "Roubo conhecido: neste gabarito conservador, 2FA sem garantia de canal independente não substitui bloqueio."),
    2: ("dano_confirmado", ("rejected",),
        "Coação conhecida: 2FA pode ser concluído sob ameaça; a operação deve ser bloqueada."),
    3: ("legitima", ("approved", "two_factor_required"),
        "Envio declarado legítimo: permitir ou confirmar identidade; rejeição é bloqueio indevido neste gabarito."),
    4: ("legitima", ("approved", "two_factor_required"),
        "Recebimento declarado legítimo: permitir ou confirmar identidade; rejeição é bloqueio indevido neste gabarito."),
    5: ("sem_verdade_terreno", (),
        "Pix genérico: não há indicação de legitimidade ou fraude para fixar um status correto."),
    6: ("suspeita_documentada", ("two_factor_required", "rejected"),
        "Um relato aberto sobre o destinatário pede ao menos verificação adicional."),
    7: ("suspeita_documentada", ("two_factor_required", "rejected"),
        "Um relato aberto sobre o remetente pede ao menos verificação adicional."),
    8: ("paridade_sem_revogacao", (),
        "O aparelho antigo continua registrado; a troca anterior não deve, sozinha, mudar o status."),
    9: ("legitima", ("approved", "two_factor_required"),
        "Movimentação declarada legítima da filial: permitir ou confirmar identidade."),
    10: ("legitima", ("approved", "two_factor_required"),
         "Mesma titularidade declarada: permitir ou confirmar identidade."),
    11: ("legitima", ("approved", "two_factor_required"),
         "Folha de pagamento declarada legítima: permitir ou confirmar identidade."),
}
EXTRA_FIELDS = ("gabarito_tipo", "status_aceitos", "comparacao", "justificativa")


def expected_applicability(case: int, row: dict[str, str]) -> bool:
    if case == 9:
        return row["origem"] == "PJ" and row["idade_origem"] == "recente" and row["tentativas"] == "multiplas"
    if case == 10:
        return row["origem"] == row["destino"]
    if case == 11:
        return row["origem"] == "PJ" and row["destino"] == "PF" and row["tentativas"] == "multiplas"
    return True


def factor_key(row: dict[str, str]) -> tuple[str, ...]:
    return (row["caso"], *(row[name] for name in FACTOR_LEVELS))


def load_matrix(path: Path) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        fields = tuple(reader.fieldnames or ())
        required = {"caso", "aplicabilidade", "status", *FACTOR_LEVELS}
        if not required.issubset(fields):
            raise ValueError(f"Matrix is missing fields: {sorted(required - set(fields))}")
        rows = list(reader)
    keys = [factor_key(row) for row in rows]
    expected = {(str(case), *values) for case in CASE_NAMES for values in product(*FACTOR_LEVELS.values())}
    if len(keys) != len(expected) or set(keys) != expected:
        raise ValueError("Matrix does not contain exactly one row for every case/factor permutation")
    return rows, fields


def compare(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    by_key = {factor_key(row): row for row in rows}
    result = []
    for row in rows:
        case = int(row["caso"])
        kind, allowed, rationale = ANSWER_KEY[case]
        expected_applicable = expected_applicability(case, row)
        actual_applicable = row["aplicabilidade"] == "avaliado"
        if row["aplicabilidade"] not in ("avaliado", "inaplicavel"):
            raise ValueError(f"Unknown applicability at {factor_key(row)}")
        if expected_applicable != actual_applicable:
            outcome = "aplicabilidade_divergente"
        elif not expected_applicable:
            outcome = "inaplicavel"
        elif row["status"] not in STATUS:
            outcome = "status_invalido"
        elif kind == "sem_verdade_terreno":
            outcome = "indeterminado"
        elif kind == "paridade_sem_revogacao":
            baseline = by_key[("5", *factor_key(row)[1:])]
            outcome = "conforme" if row["status"] == baseline["status"] else "divergente"
        else:
            outcome = "conforme" if row["status"] in allowed else "divergente"
        result.append({**row, "gabarito_tipo": kind,
                       "status_aceitos": "|".join(allowed) if allowed else
                       ("igual_ao_caso_5" if case == 8 else "indeterminado"),
                       "comparacao": outcome, "justificativa": rationale})
    return result


def summarize(rows: list[dict[str, str]]) -> dict:
    cases = {}
    for case, description in CASE_NAMES.items():
        subset = [row for row in rows if int(row["caso"]) == case]
        cases[str(case)] = {
            "descricao": description,
            "gabarito_tipo": ANSWER_KEY[case][0],
            "status_aceitos": list(ANSWER_KEY[case][1]) if case != 8 else ["igual_ao_caso_5"],
            "comparacao": dict(Counter(row["comparacao"] for row in subset)),
            "status_observados": dict(Counter(row["status"] for row in subset if row["aplicabilidade"] == "avaliado")),
        }
    return {"total_linhas": len(rows), "comparacao": dict(Counter(row["comparacao"] for row in rows)),
            "casos": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "reports/pix_permutation_matrix.csv")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports")
    args = parser.parse_args()
    rows, original_fields = load_matrix(args.input)
    comparison = compare(rows)
    summary = summarize(comparison)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "pix_permutation_gabarito.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=(*original_fields, *EXTRA_FIELDS))
        writer.writeheader()
        writer.writerows(comparison)
    (args.output_dir / "pix_permutation_comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
