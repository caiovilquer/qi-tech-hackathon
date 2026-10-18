# Aceitação integrada Pix — guia V3 §11.2

Esta matriz distingue o que é exercitado por HTTP/PostgreSQL, pelo serviço
interno e pelo motor puro. O conjunto das evidências compõe a aceitação; um
teste puro não comprova HTTP, lock, commit ou recuperação de processo.
A implementação parte da Base API; não utiliza o Simulator em runtime.

## Rastreabilidade obrigatória

Todos os caminhos são relativos à raiz do Git. O comando integral do README
executa todos os testes abaixo com PostgreSQL 16 real. Funções citadas sem
prefixo de classe podem ser selecionadas com `pytest -k NOME`.

| Grupo do guia | Evidência executável | Camada / garantia verificada |
|---|---|---|
| HTTP | `tests/integration/test_pix_api.py::test_official_http_decisions_are_committed_and_queryable`; `test_http_raw_parser_errors_leave_receipts_without_analysis`; `test_http_semantic_errors`; `test_http_auth_and_unfinished_get`; `test_receiving_chunked_body_enforces_limit_and_accepts_exact_boundary` | POST oficial, três status, topo exato, GET por ID, erros/token, parsing exato e 64 KiB por HTTP real |
| Idempotência | Mesmo arquivo: `test_replay_get_restart_and_tenant_are_independent_of_providers`; `test_http_concurrent_same_id_commits_once_and_different_hash_is_409`; `tests/integration/test_pix_api_review.py::test_two_tenants_load_through_create_app_and_http`; `tests/unit/test_pix_contract.py` | Replay sem providers, hash completo, conflito antes de enriquecimento, tenants por configuração. O teste de contrato complementa a cobertura de todos os campos |
| Concorrência | `test_same_origin_distinct_requests_are_all_counted_after_initial_wait`; `test_http_distinct_origin_and_destination_requests_serialize_fan_in`; `test_terminal_2fa_event_arriving_before_final_locks_releases_only_its_exposure`; `test_actual_process_crash_and_restart_recover_both_leases` em `test_pix_api.py`; `test_claim_while_owner_finalizes_is_busy_and_hash_conflict_does_not_wait` em `test_pix_storage.py` | HTTP/threads/processos e SQL real: IDs iguais/distintos, origem e destino compartilhados, eventos entre leitura e lock, takeover/fencing |
| Transação | `test_http_commit_failure_has_no_partial_analysis_and_unknown_commit_replays`; `test_flush_sql_failure_is_rolled_back_before_http_success`; `test_unrelated_unique_constraint_is_503_not_replay` em `test_pix_api.py`; `test_actual_named_unique_reads_only_a_completed_winner` em `test_pix_api_review.py` | Falha SQL real, constraint deferred e confirmação perdida após commit; nenhuma resposta de sucesso antes do commit. Somente a constraint correta resolve replay |
| Checagem inicial | `test_source_initial_survives_later_provider_failure_and_next_id_skips_it`; crash e concorrência HTTP citados acima; `test_concurrent_initial_checks_only_one_account_owner`; `test_operation_takeover_fences_even_an_unexpired_initial_ticket` em `test_pix_storage.py` | Resultado durável mesmo se destinatário falha; duas primeiras chamadas, leases distintas e dono antigo impedido. Ausência/timeout continuam desconhecidos |
| Tempo | `tests/integration/test_pix_delivery.py::test_delivery_scenarios_survive_process_restart_and_leave_no_database` (noite cruzando meia-noite); `test_future_knowledge_bank_execution_is_not_ingested_as_known_now`; `test_locked_security_refresh_recomputes_preliminary_recipient_and_rejects` em `test_pix_api.py`; `test_context_reads_receive_time_even_if_bank_occurrence_is_old` em `test_pix_storage.py`; `tests/unit/test_pix_policy_reference.py` e `test_pix_review_regressions.py` | HTTP/SQL para noite, conhecimento futuro/tardio e revogação; motor puro para bordas abertas/fechadas, timestamps iguais, offsets/DST e janelas. Clock do app divergente é coberto no HTTP em `test_pix_api_review.py` |
| Provider | `test_slow_providers_are_cancelled_without_blocking_http_event_loop`; `test_known_expired_bank_context_never_persists_after_three_cycles`; `test_provider_outage_is_uncertainty_not_fake_clean_or_503` em `test_pix_api.py`; `tests/unit/test_pix_provider_selection.py`, `test_pix_provider_adapters.py`, `test_pix_enrichment.py` | Timeout/cancelamento real async via HTTP; expirado impede commit; ausência não vira limpeza. Contratos verificam sujeito/tenant/escopo/versão, cache e restrição acima do corte de C_pre |
| Persistência | Crash/reinício acima; `test_delivery_scenarios_survive_process_restart_and_leave_no_database`, `test_scenario_failure_with_live_api_stops_uvicorn_and_drops_database`, `test_sigterm_during_live_api_stops_uvicorn_and_drops_database` e `test_api_startup_failure_reports_child_output` em `test_pix_delivery.py`; `tests/integration/test_migrations.py`; `test_current_migrations_on_empty_or_legacy_base_preserve_data`; `test_versioned_base_upgrades_incrementally`; `test_analysis_writer_does_not_invent_past_knowledge_and_indexes_exist` em `test_pix_storage.py` | Reabrir intérprete e recuperar mesmo JSON com fixtures diferentes, sem nova tentativa; migrations em banco vazio/legado/versionado, rollback, checksums, índices declarados e consultas por janela; limpeza da demo (banco aleatório e Uvicorn filho) em falha com API viva, SIGTERM e falha de startup com saída do filho |
| Segurança de eventos | `test_internal_execution_events_are_idempotent_linked_and_terminal`; `test_auth_is_separate_and_exact_challenge_binding_releases_exposure`; `test_late_nonterminal_failure_does_not_revive_passed_challenge`; `test_challenge_binding_requires_key_and_fingerprint_and_is_exclusive` em `test_pix_storage.py`; demo de 2FA/execução | Serviço interno sob locks: duplicata igual/conflitante, desafio/fingerprint exatos, transição terminal. Não existe endpoint HTTP de ingestão Pix; biblioteca interna recebe somente fatos de chamador institucional confiável. Não é protocolo de autenticação de webhook bancário |

## Demonstração reproduzível

```bash
docker compose -p pix-demo -f compose.test.yml run --build --rm demo
docker compose -p pix-demo -f compose.test.yml down -v --remove-orphans
```

O runner `scripts/demo_pix.py` não importa testes, não injeta outro controller e
não substitui regras. Gera JSON pelo codec de fixtures existente, carrega-o pelo
`PIX_FIXTURE_FILE` da aplicação e usa o catálogo V3 empacotado. O tempo de
conhecimento é ancorado no PostgreSQL; transações ocorrem dois dias antes,
sempre no passado, em horários fixos de dia/noite. As datas de abertura de contas
sintéticas são estáveis. O Pix seguinte ao executado é datado 25h depois dele:
pela data da transação o pagamento já estaria dentro da janela de maturidade de
24h, e ele permanece imaturo apenas porque a execução se tornou conhecida agora.

| Cenário | Resultado obrigatório |
|---|---|
| Primeiro Pix, valor pequeno | `approved`; checagem inicial `queried/not_covered`, sem fingir limpeza |
| Troca legítima | Aparelho anterior e novo explicitamente registrados aprovam; controle desconhecido acima de 20.000 exige 2FA |
| Rajada de dez IDs PF | Quatro aprovados, cinco com 2FA, décimo rejeitado; não são replays |
| Noite acumulada | Quatro Pix de 90.000: aprovado, 2FA, 2FA, rejeitado ao superar 300.000; a noite atravessa meia-noite em São Paulo |
| Destinatário adverso | Restrição vigente explícita rejeita; motivos observados: `PAYEE_DICT_ADVERSE`, `PAYEE_DICT_RESTRICTED` e `DICT_CHECK_UNAVAILABLE` (remetente não coberto). Contagem adversa sem restrição é coberta por `tests/unit/test_pix_policy_reference.py::test_adverse_counters_do_not_imply_explicit_restriction`, não pela demo |
| Replay/reinício | Encerra Uvicorn, troca fixture por dataset vazio, inicia novo intérprete; POST 200 e GET iguais ao JSON original |
| 2FA seguido de execução | Desafio vinculado; `passed` não cria pagamento. Só evento `executed` cria um pagamento, ainda não maduro no Pix seguinte 25h depois; esse Pix recebe `two_factor_required` (aparelho não cadastrado e destinatário não coberto); dispositivo continua não cadastrado e análise original fica igual |

São 21 Pix/análises e 6 checagens iniciais completas. Replays não criam Pix,
não alteram motivos e não refazem a checagem inicial. Eventos são inseridos pela
API interna de `PixStorage`, explicitamente como banco simulado confiável; o
runner não executa transferências nem verifica fatores reais de autenticação.

## Limites da evidência

- PostgreSQL é real; providers externos são adapters de fixtures. Não existe
  certificação de DICT, segurança bancária ou execução financeira real.
- A existência dos índices é testada e houve scans observados no workload da
  demo (estatísticas em `validacao-pr6.md`), sem benchmark de carga ou promessa
  de plano do otimizador para toda distribuição de dados.
- Testes de falha cobrem pontos controlados de SQL/flush/commit/processo; não
  provam disponibilidade sob qualquer falha de rede, disco ou suspensão do host.
- `APP_ENV=testing` e prefixo `pix_test_` são guardas operacionais, não autorização
  para apontar a suíte para produção. Compose separado é o caminho reproduzido.
- O runner trata SIGTERM (`docker stop`, timeout do teste) encerrando o Uvicorn
  filho e removendo o banco; SIGKILL não executa limpeza e exige `down -v`.
- Score, janelas, pisos e pesos são experimentais. Equivalência ao Python V3 é
  compatibilidade comportamental; nenhum teste mede fraude real ou calibração.
- O escopo entregue é código/API/documentação e demonstração técnica. O PDF
  também pede apresentação oral; o roteiro abaixo ajuda a prepará-la e não
  significa que ela já aconteceu.

## Roteiro sugerido para apresentação de até 7 minutos

1. Problema, três status e distinção entre score, 2FA e execução (1 min).
2. Regras e racional: contexto, limites, reputação e incerteza (2 min).
3. Executar demo, destacar troca registrada, rajada, noite e replay (2 min).
4. Mostrar evento de execução separado e persistência imutável (1 min).
5. Idempotência concorrente, testes e limites experimentais (1 min).

Reservar os 3 minutos de perguntas previstos no enunciado. Os tempos acima são
um roteiro de apresentação, não uma nova exigência técnica da API.
