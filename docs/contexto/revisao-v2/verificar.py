"""Reproduz os achados da revisão, sem subir API nem conectar a banco.

O sucesso deste script confirma as observações sobre os arquivos recebidos;
NÃO significa que a política passou em uma suíte de aceitação.

Executar a partir de qualquer diretório, com jsonschema==4.17.3 instalado.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.metadata
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import types
import warnings
from datetime import timedelta
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
ORIGINALS = Path(__file__).resolve().parent / "originais"
spec = importlib.util.spec_from_file_location("reviewed_policy_v2", ORIGINALS / "politica_v2_simulacao.py")
s = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = s
spec.loader.exec_module(s)
observations = []


def record(code, category, description, condition, observed, correction):
    if not condition:
        raise AssertionError(f"{code}: observação não reproduzida: {observed!r}")
    observations.append({
        "id": code, "category": category, "description": description,
        "observed": observed, "required_or_expected": correction,
    })


def pix(identifier, *, payer=s.A, payee=s.B, cents=5000, t=None, device="dev-1", status=None):
    return s.Pix(identifier, payer, payee, cents, t or s.at(0, 14),
                 f"account-{payee}", s.OLD, device=device, status=status)


approved = pix("approved-without-settlement", status=s.APPROVED)
challenge = pix("challenge-without-settlement", status=s.TWO_FA)
challenge.tfa = "passed"
record("P01", "problema", "Aprovação e 2FA viram pagamento efetivado sem confirmação financeira",
       s.effective(approved) and s.effective(challenge),
       {"approved_is_effective": s.effective(approved), "passed_is_effective": s.effective(challenge)},
       "Separar decisão, autenticação e execução confirmada pelo banco.")

history = []
for i in range(10):
    current = pix(f"distinct-{i}", t=s.at(0, 14) + timedelta(seconds=i))
    result = s.evaluate(current, history, s.Mocks())
    current.status = result[0]
    history.append(current)
record("P02", "problema", "Dez IDs diferentes para mesmo valor/destino escapam da frequência",
       result[0] == s.APPROVED and not any("VELOCITY_BURST" in c for c in result[3]),
       {"status": s.STATUS[result[0]], "reasons": result[3]},
       "Cada ID distinto conta; equivalência de valor/destino não é idempotência.")

past = pix("old-device", t=s.at(-2, 14), status=s.APPROVED)
first = pix("new-device-1", cents=25000, device="unknown-device")
before = s.evaluate(first, [past], s.Mocks())
first.status, first.tfa = s.TWO_FA, "failed"
second = pix("new-device-2", cents=25000, device="unknown-device", t=s.at(0, 14, 1))
after = s.evaluate(second, [past, first], s.Mocks())
record("P03", "problema", "Tentativa com 2FA falho remove o tratamento de aparelho novo",
       before[0] == s.TWO_FA and after[0] == s.APPROVED,
       {"first": s.STATUS[before[0]], "after_failed_2fa": s.STATUS[after[0]], "reasons": after[3]},
       "Histórico observado não cadastra aparelho nem confirma confiança.")

with contextlib.redirect_stdout(io.StringIO()):
    run = s.Run("background")
    run.profile(s.A, start=-200)
rent = pix("rent", payee=s.LANDLORD, cents=250000, t=s.at(-90, 10))
unbounded = s.evaluate(rent, run.hist, s.Mocks())
bounded_history = [h for h in run.hist if rent.t - timedelta(days=90) < h.t <= rent.t]
bounded = s.evaluate(rent, bounded_history, s.Mocks())
record("P04", "problema", "O horizonte SQL de 90 dias muda o cenário do aluguel",
       unbounded[0] == s.TWO_FA and bounded[0] == s.APPROVED,
       {"unbounded": {"status": s.STATUS[unbounded[0]], "score": unbounded[1]},
        "ninety_days": {"status": s.STATUS[bounded[0]], "score": bounded[1]}},
       "Simulação, consulta e critério de maturidade precisam usar a mesma cobertura.")

two_payments = [pix(f"payee-history-{i}", cents=10000, t=s.at(-5+i, 12), status=s.APPROVED)
                for i in range(2)]
result = s.evaluate(pix("payee-large", cents=100000), two_payments, s.Mocks())
record("P05", "problema", "Dois pagamentos ao favorecido não ativam a régua descrita na seção F",
       result[0] == s.APPROVED and result[1] == 0,
       {"status": s.STATUS[result[0]], "score": result[1], "reasons": result[3]},
       "A régua do favorecido precisa funcionar sem exigir o perfil geral de cinco pagamentos.")

markers = s.Mocks(dict_markers={s.B: [(s.at(-1, 14), "MULE", "psp-1")]})
stats = s.dict_stats(s.B, s.at(0, 14), markers)
record("P06", "problema", "m12 inclui marcação do mês corrente na simulação",
       stats["markers_m12"] == 1, stats,
       "O m12 documentado no DICT usa meses completos, sem o mês corrente; não 365 dias móveis.")

record("P07", "problema", "Conversão do gerador perde centavos com float",
       (int(0.29*s.R), int(1.15*s.R)) == (28, 114),
       {"0.29": int(0.29*s.R), "1.15": int(1.15*s.R)},
       "Usar centavos inteiros ou conversão exata com Decimal a partir de texto.")

calls = []
real_stats = s.dict_stats
def traced_stats(document, *args):
    calls.append(document)
    return real_stats(document, *args)
s.dict_stats = traced_stats
try:
    s.evaluate(pix("provider-calls"), [], s.Mocks())
finally:
    s.dict_stats = real_stats
record("P08", "problema", "Simulação consulta somente destinatário, não os dois lados anunciados",
       calls == [s.B], calls,
       "Não declarar evidência dict_payer=ok sem consulta/snapshot; seguir gatilhos aprovados.")

with ZipFile(ROOT / "bootcamp-base-api.zip") as archive:
    cpf_namespace = {}
    exec(compile(archive.read("bootcamp-base-api/src/utils/document_number.py"),
                 "zip:document_number.py", "exec"), cpf_namespace)
    generated = [f"2222222220{i:02d}" for i in range(20)]
    invalid = [d for d in generated if not cpf_namespace["is_valid_cpf"](d)]
    record("P09", "problema", "Os vinte favorecidos da folha têm documentos inválidos",
           len(invalid) == 20, {"invalid_count": len(invalid), "lengths": sorted({len(d) for d in invalid})},
           "Gerar documentos válidos antes de converter os cenários em testes HTTP com DV.")

    # Executa o SchemaHandler efetivamente entregue, substituindo somente imports
    # de configuração e de erro; nenhum app, middleware ou banco é inicializado.
    constants = types.ModuleType("constants")
    constants.SCHEMA_PATH = str(ORIGINALS)
    errors = types.ModuleType("errors")
    class InvalidSchema(Exception):
        pass
    errors.InvalidSchema = InvalidSchema
    saved = {name: sys.modules.get(name) for name in ("constants", "errors")}
    sys.modules.update(constants=constants, errors=errors)
    namespace = {}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            exec(compile(archive.read("bootcamp-base-api/src/utils/schema_handler.py"),
                         "zip:schema_handler.py", "exec"), namespace)
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous

version = importlib.metadata.version("jsonschema")
if version != "4.17.3":
    raise RuntimeError(f"Usar jsonschema==4.17.3, versão da base; encontrada {version}")

@namespace["SchemaHandler"].validate("post_pix_transaction.json")
def validate_payload(*, payload):
    return payload

example = {
    "id": "pix-000123", "amount": 150000,
    "transaction_date": "2026-10-17T22:15:00-03:00",
    "source_account": {"branch": "0001", "account_number": "88213",
                       "opening_date": "2025-03-02T10:00:00-03:00",
                       "owner": {"document_number": "123.456.789-09"}},
    "destination_account": {"participant": "60701190", "branch": "3675", "account_number": "10442",
                            "opening_date": "2026-10-10T09:00:00-03:00",
                            "owner": {"document_number": "987.654.321-00"}},
    "source": {"device_id": "dvc-9f2c71a8", "channel": "app", "ip": "198.51.100.23",
               "geolocation": {"latitude": -23.56, "longitude": -46.65}},
}
record("V01", "confirmacao", "Exemplo oficial passa no schema usando o handler da base",
       validate_payload(payload=example) == example, "aceito", "Manter compatibilidade com o contrato.")

variant = copy.deepcopy(example)
variant["amount"] = 150000.0
record("V02", "confirmacao", "JSON Schema aceita número integral representado como float",
       validate_payload(payload=variant) == variant, "150000.0 aceito", "Normalizar somente após validar.")
variant["amount"] = True
try:
    validate_payload(payload=variant)
except InvalidSchema:
    boolean_rejected = True
else:
    boolean_rejected = False
record("V03", "confirmacao", "Booleano não é aceito como amount", boolean_rejected,
       "true rejeitado", "Não converter bool para int antes da validação.")

variant = copy.deepcopy(example)
variant["transaction_date"] = "2026-02-30T22:15:00-03:00"
variant["source"]["ip"] = "not-an-ip"
record("V04", "confirmacao", "Schema sozinho não valida calendário nem endereço IP",
       validate_payload(payload=variant) == variant, "data inexistente e IP textual aceitos",
       "Validar datas e normalizar IP antes de persistir em INET; documentar 4xx ou ausência.")

variant = copy.deepcopy(example)
variant["id"] = "pix.000123"
try:
    validate_payload(payload=variant)
except InvalidSchema:
    restricted_id = True
else:
    restricted_id = False
record("V05", "decisao_nao_oficial", "O schema limita o alfabeto do ID além do enunciado", restricted_id,
       "pix.000123 rejeitado", "Limite próprio precisa ser declarado; x-request-id não define ID do Pix.")

internal_reference = {"type": "object", "definitions": {"x": {"type": "string"}},
                      "properties": {"value": {"$ref": "#/definitions/x"}}}
namespace["SchemaCache"].schemas[str(ORIGINALS / "internal_reference.json")] = internal_reference
@namespace["SchemaHandler"].validate("internal_reference.json")
def with_ref(*, payload):
    return payload
try:
    with_ref(payload={"value": "ok"})
except Exception as error:
    ref_error = type(error).__name__
else:
    ref_error = None
record("V06", "confirmacao", "Resolver da base falha em referência interna", ref_error == "RefResolutionError",
       ref_error, "O schema entregue sem $ref contorna esse comportamento.")

document = (ORIGINALS / "AVALIACAO-ANTIFRAUDE-PIX-v2.md").read_text()
cnpj_source = next(block for block in re.findall(r"```python\n(.*?)\n```", document, re.S)
                   if "CNPJ_PATTERN" in block)
cnpj_namespace = {"re": re}
exec(compile(cnpj_source, "evaluation:cnpj", "exec"), cnpj_namespace)
cnpj_result = {value: cnpj_namespace["is_valid_cnpj"](value) for value in
               ["12.ABC.345/01DE-35", "11.222.333/0001-81", "11.222.333/0001-82", "00000000000000"]}
record("V07", "confirmacao", "Exemplos do validador CNPJ reproduzem os resultados anunciados",
       list(cnpj_result.values()) == [True, True, False, False], cnpj_result,
       "DV correto não comprova existência cadastral, titularidade ou regularidade.")

baseline = subprocess.run([sys.executable, str(ORIGINALS / "politica_v2_simulacao.py")],
                          capture_output=True, text=True, check=True)
(ROOT / "revisao-v2" / "saida-simulacao-recebida.txt").write_text(baseline.stdout)
files = ["AVALIACAO-ANTIFRAUDE-PIX-v2.md", "politica_v2_simulacao.py",
         "post_pix_transaction.json", "bootcamp-base-api.zip"]
report = {
    "scope": "Reprodução local da simulação e validação isolada do schema, sem HTTP/PostgreSQL",
    "meaning_of_success": "Observações reproduzidas; não é aprovação da política.",
    "jsonschema_version": version,
    "input_sha256": {name: hashlib.sha256(((ROOT if name.endswith('.zip') else ORIGINALS) / name).read_bytes()).hexdigest() for name in files},
    "observations": observations,
}
(ROOT / "revisao-v2" / "resultados.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
for item in observations:
    print(f"{item['id']} [{item['category']}] {item['description']}")
print(f"\n{len(observations)} observações reproduzidas; isto NÃO aprova a política.")
