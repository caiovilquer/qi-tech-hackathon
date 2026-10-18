# Revisão final — logs, erros, legibilidade e fluxo entre camadas

Branch `chore/observability-readability`, base `main` em `dd1d4b5` (merge da PR #7).
Objetivo: alinhar o projeto aos critérios habituais de avaliação da QI Tech em testes de
caixa-preta: logs coerentes e suficientes para explicar qualquer saída, erros mapeados com
clareza, código legível sem comentários explicativos, nomes que indicam função e camadas
sem acessos indevidos. Sem alteração de contrato HTTP, política, migrations ou dependências.

## Achados da análise

### Logs e observabilidade

- O fluxo Pix só registrava erros, todos com `logger.error` **sem a exceção**: um 503
  aparecia como "Pix analysis could not be committed reliably" sem causa nem traceback,
  porque `PixError` é tratado pelo handler de `QIException`, que não loga.
- Não existia linha de decisão: nenhum registro de tenant, ID do Pix, status, score,
  motivos, ciclos de revalidação ou tempo por requisição. Replays, 409 e 503 eram
  indistinguíveis no log.
- Esperas por reserva (`Busy`), ciclos de revalidação (`Refresh`/`EnrichmentNeedsRefresh`),
  indisponibilidade de provider e estouro de orçamento (`BudgetExceeded`) eram silenciosos.
- Mensagens misturavam português (`ENTROU`/`SAIU`, "Erro inesperado em") e inglês.
- O access log do Uvicorn duplicava as linhas do request logger, sem o request id.

### Erros

- `PixError` usava um título único ("Pix request error") e uma tradução genérica para
  nove códigos; o cliente não distinguia a natureza do erro sem consultar a documentação.
- Erros de validação (400/422) não indicavam o campo.

### Comentários e nomes

- Arquivos da base carregavam ensaios didáticos: `app.py` (86 linhas de comentário),
  `database.py`, `session_manager.py` (docstring de 170 linhas), `request_context.py`,
  `schema_handler.py`, `sample_entity.py`, entre outros.
- Módulos Pix usavam nomes de uma letra em todo o fluxo: `r`, `m`, `t`, `p`, `f`, `h`,
  `c`, `e`, `fp`, `hash_`, propriedades `f.p`, `f.t`, `f.ns`, campos `mine`, `count`,
  `day`, `exposed`, `sec_valid`; helpers `identity`, `binding`, `scope`, `local`, `_winner`.

### Fluxo e acessos entre camadas

- Confirmado sem acesso indevido: os middlewares não abrem sessão nem executam SQL
  (`session_manager` só cria o contexto vazio; `internal_token` nega o 403 antes de
  qualquer sessão; `/` e `/health_check` não dependem do banco).
- `PixStorage` (camada de coordenação/controller) executava SQL diretamente em ~25
  pontos, enquanto a base estabelece que query mora no repository.
- O middleware de autenticação importava `resolve_tenant` do módulo do controller
  (`controllers.pix_api`): dependência de middleware para controller.
- O resource Pix contém a leitura limitada do corpo e o recibo; é lógica de borda HTTP,
  mantida, mas com nomes explícitos.

## Mudanças

- **Logs:** `utils/logger.py` ganha `log_fields` (`chave=valor`, escapado, sem `None`).
  Eventos: `request.started`/`request.finished`, `auth.denied`, `error.response`,
  `error.http`, `error.unexpected`, `pix.analysis.completed` (status, key, score,
  `confidence_pre`, motivos, ciclos, `elapsed_ms`), `pix.analysis.error` (status, código,
  campo), `pix.analysis.budget_exceeded`, `pix.analysis.reconciliation_required`,
  `provider.unavailable`, `pix.query.not_found`, e falhas técnicas com traceback
  (`pix.analysis.failed`, `pix.analysis.database_error`, `pix.receipt.*`,
  `pix.race.winner_unreadable`, `pix.reservation.release_failed`). Esperas e ciclos em
  DEBUG. Uvicorn sem access log no Dockerfile. Tabela no README.
- **Erros:** `errors/pix.py` passa a um catálogo por código com status, título,
  descrição e tradução próprios; o construtor valida a coerência status/código e aceita
  o campo inválido (`Field: …` na descrição de 400/422).
- **Camadas:** todo SQL do Pix migrou para `PixRepository` (métodos nomeados:
  `claim_state`, `reserve_claim`, `take_over_claim`, `release_claim`, `reserve_initial`,
  `complete_initial`, `record_analysis`, `update_open_receipt`, `challenges`,
  `events_of_kind`, …); `PixStorage` só coordena transações, locks e regras de
  consistência. `Tenant`, `load_tenants` e `resolve_tenant` foram para
  `utils/pix_tenants.py`; o middleware não importa mais o controller.
- **Nomes:** `features`/`policy`/`params` nas regras; campos `payer_history`,
  `general_profile`, `recipient_established`, `device_state`, `trusted_beneficiary`,
  `destination_age`, `night_qualifies`, `day_attempts`, `day_amount_cents`, `exposures`,
  `night_started_at`, `velocity_count`, `security_valid`; `validate_identity`,
  `validate_binding`, `require_bank_reference`, `is_lock_timeout`, `analysis_row`,
  `_completed_analysis`, `_linked_analysis`, `LEASE_DURATION`; controller com
  `_read_context`, `_persist_enrichment`, `_finalize_analysis`, `ContextRefreshRequired`,
  `request_deadline`; resource com `MAX_BODY_BYTES`, `handed_to_controller`,
  `declared_length`; `models/pix.py` sem aliases de uma letra. Chaves JSON do snapshot
  de auditoria e o contrato público não mudaram.
- **Comentários:** ensaios removidos da base e dos módulos Pix; docstrings de uma a três
  linhas dizem o que a peça faz e o invariante que protege.
- **README:** reorganizado no modelo do PDF, com tabela de erros, tabela de eventos de
  log e comandos de consulta por request id.

## O que não mudou

Contrato HTTP, política `pix-experimental-v3`, migrations, dependências, protocolo de
locks/leases, comportamento dos testes (apenas nomes internos foram atualizados nos
testes que os referenciam). A comparação com a referência V3 continua equivalente.

## Execução

Linux x86_64, Docker 29.8.2, Compose v5.6.0, PostgreSQL 16 e Python 3.11 nas imagens.

1. Primeira build completa sem mounts sobre o código refatorado: **447 passed, 1 failed em
   89,22 s**. A falha foi `test_terminal_auth_failure_releases_only_exact_challenge_exposure`,
   que lia o campo renomeado `Features.exposed`; o teste passou a usar `exposures`.
   Nenhuma falha de comportamento.
2. Foco após a correção (`test_pix_storage.py` + `test_pix_log_escape.py`, `tests/` montado):
   **56 passed em 10,81 s**.
3. Demo em projeto novo (`run --rm demo`): exit 0, 21 análises, 6 checagens,
   `follow_up_status: two_factor_required`, `payment_older_than_maturity_window: true`.
4. Comparação com a referência V3: 59 observações e 2.000 contextos equivalentes.
5. Amostra real contra a API do Compose de testes (POST novo, replay, conflito, JSON
   inválido, DV inválido, GET inexistente, token errado): 201/200/409/400/422/404/403 com
   títulos próprios, `Field: amount` e `Field: source_account.owner.document_number` nas
   descrições, e no log `pix.analysis.completed … status=approved score=20
   confidence_pre=50 reasons=RECENT_PAYEE_ACCOUNT,… cycles=1 elapsed_ms=…`,
   `pix.analysis.error … http_status=422 code=QIT002003 field=source_account.owner.document_number`,
   `pix.query.not_found`, `auth.denied` e `error.response`, todos com o mesmo
   `X-Request-ID` devolvido ao cliente.
6. Build completa final sem mounts, projeto novo (`pix-obs2`): **448 passed, 1 warning em
   93,73 s**, exit 0, sem skips/xfails. Stacks removidas com `down -v --remove-orphans`.
