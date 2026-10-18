# Correções da revisão independente — PR 3

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/3
Branch: `feat/pix-providers`.
SHA originalmente revisado: `5e2852c239dbd35695eb8030188268d3ac599beb`.
O novo SHA exato está na descrição da PR e nas cinco respostas individuais.
Para re-revisão: `git diff 5e2852c..HEAD`, registrando o HEAD efetivamente lido.

Os cinco achados foram conferidos contra o código e o guia. Os três bugs foram
corrigidos e as duas melhorias defensivas foram adotadas nesta mesma PR.
Nenhum achado foi descartado ou transferido para outra etapa; aguardar a
re-revisão e aprovação, sem resolver threads ou fazer merge automaticamente.

## Achados

| Thread | Classificação | Estado e correção | Regressão |
|---|---|---|---|
| [4306055725](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306055725) | P2-1, bug | Corrigido: relógio UTC injetável no Enricher, recebimentos após consultas e decision_at final. `as_of` é corte de fixture/despacho, não promessa de leitura histórica remota. `security_lookup` preserva BankRecord bruto e `received_at` separa recibos locais dos tempos da fonte. Cobertura é reavaliada ao final. | Adapter ao vivo devolve revoked e fraude após o início e produz rejeição DEVICE_REVOKED; relógio determinístico confirma ordem de recibos, pagamentos conhecidos durante a consulta e integração ao perfil; futuro continua excluído; segurança expirada não vira vigente; regressão de relógio falha. |
| [4306055728](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306055728) | P2-2, bug | Corrigido: initial_result usa o resultado selecionado da origem, coerente com Context, não a resposta limpa antiga. lookups conserva separadamente a resposta bruta. | Watermark adverso vencido + limpeza antiga produz initial_result stale/adverso. Com outage, initial_result conserva watermark e lookup conserva unavailable. Evidência futura gera initial_result not_covered. |
| [4306055731](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306055731) | P3-1, bug | Corrigido: normalizador rejeita device apenas whitespace com 400/QIT000001/source.device_id; Pix puro também rejeita. Device não vazio preserva a identidade sem strip. | Espaços, tab/newline e whitespace Unicode recusados na fronteira; identidade válida com Unicode/espaços nas pontas preservada. |
| [4306055732](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306055732) | P3-2, defesa | Adotado: Context recusa identidade bancária duplicada/conflitante, evento reutilizado e sobreposição com histórico local/operação atual. Reconciliar antes de construir o Context final; não deduplicar silenciosamente fatos conflitantes. | Mesmo BankPayment repetido, mesma operação com valor diferente, evento em duas operações, colisão local/atual; mesmo ID em providers distintos continua permitido pelo namespace. |
| [4306055733](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306055733) | P3-3, contrato | Adotado: OSError/connection reset/refused são falhas técnicas com até um retry e cobertura unavailable após esgotamento; Protocols exigem tradução de falhas específicas do SDK para ProviderUnavailable. Dados inválidos e bugs inesperados não são escondidos como outage. | Banco reset/refused faz duas tentativas por método e devolve unavailable; reset seguido de sucesso recupera; ProviderDataError/ValueError/RuntimeError propagam sem retry. Cancelamento e deadline continuam nos testes anteriores. |

## Temporalidade e limites das correções

O guia §§4.2 e 8.2 exige decisão posterior ao enriquecimento; o código anterior
usava o instante preliminar como cutoff definitivo. O motor segue puro: só o
Enricher lê relógio, que é injetado nos testes históricos. Datas da fonte,
recibo local e tempo da decisão são distintos. `security` é a seleção final,
`security_lookup` o bruto, `lookups` as respostas DICT brutas e `initial_result`
a seleção da origem. Cache não renova a validade de um fato expirado.

Se um cache usado vencer durante outra consulta, ou C_pre final exigir uma
consulta antes pulada, EnrichmentNeedsRefresh interrompe a montagem. O futuro
coordenador deve revalidar dentro do mesmo deadline/ciclos do guia; não há
loop transacional ou resultado inicial durável nesta etapa. Essa exceção não
é uma quarta decisão antifraude e não deve virar aprovação com dados antigos.

Device apenas whitespace passa a ter rejeição semântica local antecipada.
Isso alinha os domínios do normalizador/Pix/SecurityScope; não é exigência
atribuída ao PDF nem trim de identidade. Schema oficial e fontes preservados.
Não há novo endpoint ou alteração do formato público nesta rodada.

## Observações gerais da revisão

- Links bancários completos continuam pré-condição do chamador. Alias omitido
  entre IDs distintos não pode ser inferido por valor/horário; sobreposição de
  ID local sem link continua recusada. A PR 4 deverá consultar vínculos completos.
- Mesma pessoa nos dois papéis reaproveita a consulta; sucesso disponível aparece
  como cache no segundo papel, falha permanece queried. É metadado honesto,
  já coberto, sem criar outra contribuição de risco ou campo público extra.
- Revogação vencida permanece stale/unknown no motor. O futuro coordenador HTTP
  deve recusar contexto sabidamente vencido/revalidar; não foi implementado
  um endpoint artificial para declarar essa garantia nesta PR.
- Cache de segurança de outra chave agora é ignorado no Enricher, assim como
  cache de reputação de outra chave. Resposta bruta fora do escopo e entradas de
  cache com tipo inválido continuam erro. A seleção pura permanece estrita.

## TDD e validação

Antes da alteração de produção, 11 testes novos reproduziram **oito falhas de
assert e três erros**. Incluem os dois P2, falhas de transporte não traduzidas,
blank device e validação defensiva de Context. A rodada ampliada de 17 métodos
registrou oito falhas e dez erros; os erros adicionais incluem ausência do
novo parâmetro clock/da exceção, não novos bugs comportamentais. Dois casos
suplementares de vencimento/outage foram acrescentados durante a revisão
própria. Total final: **19 métodos novos** em test_pix_provider_review.py.
Testes históricos existentes passaram a injetar BASE no relógio; o teste de
adapter ao vivo usa o relógio real e respostas após await curto.

Validação em 18/10/2026, Python 3.11.15/macOS ARM64 e Docker:

- **214 testes puros passaram** (195 anteriores +19 regressões).
- **297 testes passaram em 5,00 s** na suíte completa, sem skips/xfails,
  com PostgreSQL **16.15** real: 83 HTTP/PostgreSQL da base +214 puros.
- As **19 regressões** também passaram isoladas no container via pytest.
  Uma invocação manual via unittest sem PYTHONPATH falhou ao importar antifraud;
  foi corrigido o comando para pytest, que aplica pythonpath=src do projeto.
- **92 testes originais passaram**; **59 observações** e **2.000 contextos**
  continuam iguais à Decision V3 completa, incluindo comparação em container.
- Exemplo executável da documentação reproduzido, 24 hashes documentais
  conferidos, compilação e git diff --check passaram. Revisão própria do diff.

A suíte PostgreSQL continua validando a base/migrations; não prova persistência,
idempotência ou concorrência Pix, que ainda não são entregues. O revisor
registrou limitações de Python 3.14 no seu ambiente; aqui e no CI usa-se 3.11
fixado. Não foram alteradas dependências para contornar ambiente fora da versão.

Na raiz do checkout, com Docker disponível:

```bash
docker compose -p pix-pr3-rereview -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr3-rereview -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/unit/test_pix_provider_review.py
docker compose -p pix-pr3-rereview -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr3-rereview -f compose.test.yml down -v --remove-orphans
```

Com venv de desenvolvimento:

```bash
PYTHONPATH=src .venv/bin/python -m unittest tests.unit.test_pix_provider_review -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

## Prompt de re-revisão

Revise a mesma PR 3, branch feat/pix-providers, no novo SHA exato registrado
na descrição/respostas. Leia este relatório, docs/REVISAO-PR3.md e o diff desde
5e2852c. Verifique os cinco achados com as reproduções originais e os testes
novos; procure regressões de temporalidade, dados brutos/selecionados,
vencimento, cache por escopo, identidade bancária e exceções. Não considere
os testes verdes prova de ausência de bugs. Para cada achado, cite prioridade,
arquivo/linha, requisito, gatilho e evidência; diferencie requisito futuro de
lacuna real da etapa. Não altere código, resolva threads, faça merge ou avance.
