# Correções da revisão independente — etapa 4 (storage)

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/5
Branch: `feat/pix-storage`. SHA revisado: `7fead974c96c353229ae03c78586b60fc6501432`.
O novo SHA exato fica na descrição da PR e nas respostas individuais; para
re-revisão, comparar `git diff 7fead97..HEAD` e registrar o HEAD efetivamente lido.

A revisão foi estática, com sondagens puras em Python, porque a máquina do revisor
não tinha Docker/PostgreSQL. Encontrou um bug de integração entre a etapa 3 e o
writer (P2), três achados P3, uma lista de caminhos sem teste e cinco itens
opcionais. Todos foram corrigidos ou decididos nesta mesma PR; nenhum foi
transferido a outra etapa. Aguardar re-revisão e aprovação explícita antes de
resolver threads ou fazer merge.

## Achados, decisões e regressões

| # | Prioridade | Achado | Decisão e correção | Regressão |
|---|---|---|---|---|
| 1 | P2, bug | `complete()` exigia igualdade exata de `execution_events`, mas a reconciliação da etapa 3 acrescentava ao Context uma segunda cópia de cada execução bancária vinculada (mesmo provider/ID, `available_at` do banco diferente do `received_at` local). Nenhuma análise com `bank_links` era persistível; omitir links contaria o pagamento duas vezes. | Fato único: `reconcile_payments` reconhece execução já conhecida localmente pelo mesmo provider/ID e não a duplica (o recebimento local é metadado de conhecimento). O writer compara histórico/eventos como multiconjuntos e distingue "fato omitido/desatualizado" de "execução não persistida localmente", exigindo que o coordenador ingira as execuções vinculadas antes da transação final. Protocolo documentado em storage-pix.md. | `test_linked_bank_execution_is_one_fact_through_enricher_and_writer` (Enricher real com `FixtureBankProvider`, `bank_links` e writer; sem a correção a reconciliação devolve dois eventos e o writer recusa), `test_writer_distinguishes_unpersisted_executions_and_clock_bounds`, unitário `LinkedExecutionDedupTests`. |
| 2 | P3, bug | `claim()` com o dono em transação final bloqueava no `FOR UPDATE` e saía com `DBAPIError` 55P03 em vez de `Busy`; hash diferente só virava 409 após o commit do dono. | Leitura prévia sem lock responde `HashConflict`/`Replay` de imediato; a espera pela linha até `lock_timeout_ms` (parâmetro novo de `claim`, `claim_initial`, `complete_initial`) é mapeada para `Busy`. Lock timeout em `analysis()` continua técnico (55P03), como já testado. | `test_claim_while_owner_finalizes_is_busy_and_hash_conflict_does_not_wait`. |
| 3 | P3, bug | `bind_challenge` aceitava `key=None` e pulava a conferência da análise; fingerprint malformado só falhava por NOT NULL; provider inválido saía como `ProviderDataError` sem código. | Validação de fronteira única (`binding`/`bank_reference`): key UUID obrigatória, fingerprint hex de 64, provider pelo padrão de `ProviderIdentity`, identificadores imprimíveis até 128; tudo `InvalidEvent` QIT002007 antes de SQL, também em `link_bank_operation`. | `test_challenge_binding_requires_key_and_fingerprint_and_is_exclusive` (inclui replay e vínculo exclusivo por análise e por desafio). |
| 4 | P3, integração | O writer compara `decision_at` com o relógio PostgreSQL enquanto o Enricher usa o relógio da aplicação; o protocolo de substituição sob locks estava implícito. | `work.now()` explícito; mensagens distintas para decision_at anterior aos locks e à frente do relógio do banco; protocolo por ciclo documentado. Sem tolerância de skew: mascarar o descompasso mudaria cortes de conhecimento sem aviso, coerente com a decisão da PR 3. | `test_writer_distinguishes_unpersisted_executions_and_clock_bounds`; o teste do achado 1 usa `work.now()` após enriquecimento real. |
| 5 | P3, testes ausentes | Sem cobertura de segurança vencida, `raw_body`, replay/exclusividade de desafio e dupla finalização de receipt. | Testes adicionados. Esses caminhos já estavam corretos: os testes começaram verdes e documentam comportamento, não bugs. | `test_known_expired_security_is_refused_by_writer`, `test_raw_body_is_optional_bounded_and_bound_to_the_canonical_hash`, `test_challenge_binding_requires_key_and_fingerprint_and_is_exclusive`, `test_receipt_is_finalized_once`. |
| 6 | P4, opcional | `context()` carregava 90 dias de linhas de outros pagadores ao mesmo destino; as features só usam 24 h (`incoming`). | Adotado: constante `FAN_IN_WINDOW` no motor (mesmo valor literal de antes, sem mudança de decisão) e janela de 24 h no ramo `destination` do SQL. `history_count` da auditoria deixa de contar linhas que nenhuma feature usava. | `test_destination_history_is_limited_to_the_fan_in_window`. |
| 7 | P4, opcional | `RecordConflict` em `complete_initial` deixava a conta sem conclusão da inicial. | Mantido como erro, documentado: dado conflitante não vira cache nem indisponibilidade inventada; a reserva fica com o dono para nova tentativa ou vence para takeover. O Enricher já falha antes quando consulta o cache conflitante. | `test_initial_completion_conflict_leaves_reservation_with_its_owner`. |
| 8 | P4, opcional | `unpack` de conteúdo malformado lançava `TypeError`/`AttributeError`. | Adotado: todo conteúdo malformado é `ValueError`, com o tipo whitelisted no texto. | `test_codec_reports_malformed_stored_content_as_value_error`. |
| 9 | P4, opcional | `read()` sem `statement_timeout`. | Adotado: leitura e escrita compartilham a configuração (READ COMMITTED, lock 1 s, statement 3 s). | `test_read_transactions_are_bounded_and_read_committed`. |
| 10 | P4, opcional | Erros de `ProviderIdentity`/`identity()` em vínculos sem código. | Coberto pelo achado 3. | idem. |

Usabilidade acrescentada sem mudança de contrato: `ClaimTicket` e `InitialTicket`
carregam `lease_until` (relógio PostgreSQL), permitindo ao coordenador planejar a
transação final dentro da lease sem consultar o banco; a linha continua sendo a
autoridade. Os limites de integração registrados pela revisão (troca legítima de
titular sem API de reconciliação, lease sem renovação, eventos simultâneos de fonte
sem versionamento) estão documentados em storage-pix.md.

## Evidências

Os testes foram escritos antes das correções, a partir dos achados. Na revisão,
a máquina do revisor não tinha Docker/PostgreSQL e usava Python 3.14, fora da
versão fixada. A primeira evidência de execução foi o CI do commit de
correção `308488ecaf062109b35708629f058c6c1f56a7bb`:
[push](https://github.com/caiovilquer/qi-tech-hackathon/actions/runs/39113851445) e
[PR](https://github.com/caiovilquer/qi-tech-hackathon/actions/runs/39113853998),
ambos com **366 testes passando em 14,24 s** (354 anteriores, 11 novos de storage e
1 unitário de seleção), 1 warning do declarative_base, PostgreSQL 16 real, e
**59 observações e 2.000 contextos** iguais à Decision V3 completa.

Na máquina do revisor, sem PostgreSQL, foram conferidos: compilação de `src`, `tests` e
`migrations`; importação do módulo de storage; os dois testes de codec; o
unitário `LinkedExecutionDedupTests` e a suíte de seleção da etapa 3, em venv
descartável. Isso não substitui a suíte PostgreSQL.

Validação local final em **18/10/2026, 06:08 -03**, no checkout limpo do SHA
`8f341d6e5299f9df4e7afc848cfa3136f9cda9d2`, executando os comandos abaixo:

| Execução | Resultado observado |
|---|---|
| Suíte completa, incluindo HTTP, migrations e concorrência PostgreSQL | **366 passaram em 8,08 s**, sem skips/xfails |
| Storage e seleção de providers, isolados da suíte completa | **81 passaram em 3,08 s**, sem skips/xfails |
| Comparação com referência V3 montada somente para leitura | **59 observações e 2.000 contextos** com Decision completa idêntica |
| Limpeza da stack de testes | `down -v --remove-orphans` concluído; ambiente `pix-pr1` preservado |

Ambiente: macOS ARM64, Docker **29.8.2**, Compose **5.6.0**, Python **3.11.17**
no container e PostgreSQL **16.15** real. As duas execuções pytest terminaram
com código 0 e o mesmo warning de depreciação do `declarative_base` original;
a comparação e a limpeza também terminaram com código 0. Os logs estão fora
do Git, em `../tmp/pr4-review-local-{full,focused,reference,cleanup}.log`.
Esta rodada não alterou código nem ampliou a cobertura para o fluxo HTTP Pix;
o HEAD final da PR está na descrição.

```bash
docker compose -p pix-storage-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-storage-review -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/integration/test_pix_storage.py tests/unit/test_pix_provider_selection.py
docker compose -p pix-storage-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-storage-review -f compose.test.yml down -v --remove-orphans
```

## Prompt para re-revisão

Revise a PR #5, branch `feat/pix-storage`, no novo SHA da descrição, comparando
`7fead97..HEAD`. Leia este relatório, docs/storage-pix.md e docs/validacao-pr4.md.
Verifique: reconciliação de execução vinculada sem duplicar fato nem contar duas
vezes; writer recusando contexto pré-lock, fato não persistido e segurança vencida
com mensagens distintas; `claim` respondendo Busy/HashConflict/Replay com o dono em
transação final; validação de vínculos; janela de 24 h do destino sem alterar
decisões; lease_until nos tickets. Testes verdes não provam ausência de bugs. Não
altere código, resolva threads, faça merge ou avance etapas.
