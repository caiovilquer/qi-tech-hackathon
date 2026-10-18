# Correções da revisão independente — PR 2

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/2
Branch: `feat/pix-engine`
SHA originalmente revisado: `c6b7eb5feac1ef0de90901e9568ecf26a95cc082`.
O novo SHA exato está na descrição da PR e nas respostas de cada thread;
revisar `git diff c6b7eb5..HEAD`, registrando o HEAD efetivamente analisado.

Os nove achados foram verificados contra código e guia e são procedentes.
Todos estão corrigidos nesta PR, sem transferir bug obrigatório para outra
etapa. A sugestão opcional de DTO também foi adotada. As respostas individuais
no GitHub complementam este relatório; os threads ficam para o revisor avaliar.
Sem merge, providers, endpoints ou persistência Pix nesta rodada.

## Achados e evidências

| Thread | Prioridade | Estado e correção | Regressão |
|---|---|---|---|
| [4305881072](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881072) | P2 | Corrigido: frequência e adverso do mesmo documento aplicam o máximo entre pesos acionados. O segundo motivo recebe apenas o incremento positivo; tetos/pisos preservados. | `UniqueContributionTests`: pesos assimétricos/zero/acima do teto, cada relógio sozinho e juntos, pisos com zero, papéis invertidos, sujeitos distintos e evidência expirada. |
| [4305881075](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881075) | P2 | Corrigida lacuna de cobertura; lógica de recuperação já estava correta, sem alterar sua janela/validade. Ramo stale do DTO coberto. | Sete `RecoveryBoundaryTests`: 24h exatas/1µs dentro, agora/futuro, conhecimento tardio, snapshot expirado, múltiplos eventos/peso, relógios distintos, DST/UTC. `AuditRegressionTests` verifica stale. |
| [4305881076](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881076) | P3 | Corrigido: `NormalizedPayload.invalid_ip_raw` é imutável e integra apenas auditoria privada. Hash e payload canônico externo mantidos. | Dois IPs inválidos produzem payload igual/hash distinto, ambos reproduzíveis pelo envelope auditado; IP válido também reproduz hash. Público não vaza texto bruto. |
| [4305881079](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881079) | P3 | Corrigido: ações explícitas concordam com `dict_queried`; cache exige evidência vigente. Ação omitida pode ser inferida do conjunto de consultas, preservando metadados parciais coerentes. | `ContextConsistencyTests`: consulta sem papel, skip/cache contraditório, cache ausente/indisponível/expirado, consulta verdadeira sem evidência aplica +10. |
| [4305881082](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881082) | P3 | Corrigido: tipo de conta deve corresponder ao papel. `both` exige `account=None`, significando restrição ao documento; conta específica usa papel explícito. | `RestrictionScopeTests`: tipos incompatíveis e both ambíguo rejeitados; escopos válidos aplicam; outra conta/tenant do tipo correto continua não aplicável. |
| [4305881085](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881085) | P3 | Corrigido: `Pix.device_id` obrigatório/não vazio; `DictEvidence.provenance` obrigatório por keyword/não branco. Defaults sintéticos ficam só nas fixtures. | `RequiredOriginTests`; todas as fixtures/chamadas atualizadas explicitamente. |
| [4305881087](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881087) | P3 | Corrigido: variável opcional comentada no `.env.example`; padrão relativo ao pacote funciona no host/imagem. Compose mantém seu caminho interno. | Subprocesso importa aplicação a partir de `src/` com todos os valores do `.env.example`, sem PostgreSQL no host. |
| [4305881092](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881092) | P3 | Corrigido: hora ≤23, minuto/segundo ≤59 verificados antes de `fromisoformat`, com 422/QIT002004. Schema estrutural preservado. | Parser permissivo emulado: 24h/25h/minuto60/segundo60 nos três campos de data; nenhum é normalizado silenciosamente. Não se declara execução no Python 3.14. |
| [4305881095](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881095) | P3 | Corrigido: documentação descreve exatamente as dimensões do gerador de 2.000 contextos e separa os invariantes unitários. | Inspeção de `verify_pix_reference.py`, reprodução diferencial e rastreabilidade atualizada em `validacao-pr2.md`. |
| [4305881100](https://github.com/caiovilquer/qi-tech-hackathon/pull/2#discussion_r4305881100) | Opcional | Adotado: `PixTransactionDTO.from_decision(..., decision)` recebe decisão pronta. DTO não importa nem executa `evaluate`; rejeita versão incompatível. | `DTOFormattingTests` e testes de snapshot atualizados; chamador conserva o objeto Decision para futura persistência. |

## TDD e validação executada

Baseline do SHA revisado: **103 testes puros**, e **186 testes HTTP/PostgreSQL
+ puros em 4,70 s**, sem skips/xfails. Projeto Docker descartável
`pix-pr2-review-baseline`, sem portas/volume do ambiente de uso.

Antes de alterar produção, criados os 28 métodos de regressão em
`tests/unit/test_pix_review_regressions.py`. Execução no código anterior:
**37 falhas de subcasos e quatro erros**, reproduzindo os defeitos e a ausência
da interface de formatação proposta. São contagens de asserts/subcasos, não
41 bugs. Sete testes de recuperação e outros invariantes já corretos começaram
verdes; não se inventou mudança funcional para fabricar um ciclo red/green.

Após correção mínima e adaptação explícita das fixtures:

- **131 testes puros passaram**, incluindo os 28 novos.
- **214 testes passaram em 4,85 s** com PostgreSQL 16.15 real, sem skips/xfails;
  incluem os 83 anteriores da base/migrations e 131 puros. Check do novo SHA
  no GitHub complementa a evidência local, sem confundir banco da base com Pix.
- **92 testes originais passaram** separadamente. **59 observações** e
  **2.000 contextos** continuam idênticos à Decision V3 completa com defaults.
  As fontes originais não foram editadas. O revisor relatou uma campanha
  externa de 60.000 contextos; isso não é apresentado como execução nossa.
- Mutação pontual em memória: substituir o avaliador de recuperação por um
  que nunca retorna motivo faz os **sete testes de recuperação falharem**;
  nenhum erro de infraestrutura. Isso verifica essa lacuna específica, sem
  alegar campanha ampla de mutation testing.

Com venv de desenvolvimento instalado, na raiz do Git:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

Stack descartável e comparação documental sem subir dependências de novo:

```bash
docker compose -p pix-pr2-rereview -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr2-rereview -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr2-rereview -f compose.test.yml down -v --remove-orphans
```

Para reproduzir a mutação pontual sem editar produção:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import io
from dataclasses import replace
from unittest import TestLoader, TextTestRunner
from unittest.mock import patch
from antifraud.rules import CRITERIA
from tests.unit.test_pix_review_regressions import RecoveryBoundaryTests
criteria = dict(CRITERIA)
criteria['RECENT_ACCOUNT_RECOVERY'] = replace(criteria['RECENT_ACCOUNT_RECOVERY'], evaluator=lambda f,p,a: None)
with patch('antifraud.engine.CRITERIA', criteria):
    result = TextTestRunner(stream=io.StringIO()).run(TestLoader().loadTestsFromTestCase(RecoveryBoundaryTests))
assert len(result.failures) == result.testsRun == 7 and not result.errors
print('Mutação detectada pelos sete testes de recuperação')
PY
```

## Nits opcionais sem thread

| Sugestão | Decisão e justificativa |
|---|---|
| Diferencial subir db/migrate/api desnecessariamente | Adotada: `run --no-deps` no workflow e nos comandos documentados. O teste documental não usa banco. |
| Validar monotonicidade de pares de pontos | Não adotada: guia §9 exige pesos não negativos e **cortes** ordenados, não impõe ordem entre pesos. Não restringir configuração aceita sem requisito; os testes assimétricos agora garantem contribuição única nas duas direções. |
| Indexar eventos para reduzir O(H·E) | Não adotada nesta rodada: sugestão de desempenho, sem falha de correção ou limite de volume/SLA entregue pela biblioteca. Não há endpoint Pix nesta PR. Eventual otimização deve ter medição e preservar as invariantes temporais, sem alegar desempenho hoje. |
| Rodar os 20 OrchestrationTests contra harness portado | Não adotada como expansão desta rodada: os 92 originais continuam executáveis e não são evidência de produção; restrição com C_pre alto e evidência expirada têm cobertura em PolicyTests; expiração de adverso por papel também está nos novos UniqueContributionTests. Não se declara testado o protocolo de cache de produção. O protocolo de orquestração continua pertencendo às etapas futuras, sem declarar aqueles testes prova de banco/concorrência. |

## Limites e re-revisão

A assinatura do DTO e os construtores obrigatórios mudaram apenas na biblioteca
interna, sem consumidor HTTP Pix existente; todos os consumidores atuais nos
testes foram adaptados. O chamador fornece a decisão do mesmo Context/Policy;
o DTO é formatador, não reexecuta regras para auditar o chamador.
Schema, hashes de requests, três status, defaults V3, fluxo HTTP da base,
migrations e fontes fornecidas preservados. Sem nova dependência.

Re-revisão: confira cada thread e os asserts novos, em especial a diferença
incremental dos pesos, cache vigente/ações parciais, restrições `both`,
privacidade/reprodução do hash e os controles de horário. Procure regressões
nos novos contratos internos e preserve os limites da etapa. Testes verdes
não comprovam ausência de bugs. Não resolver threads, aprovar, fazer merge ou
avançar à próxima PR automaticamente; aguardar o revisor e o usuário.
