# Correções da re-revisão independente — etapa 5

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/6
Branch: `feat/pix-api`. SHA re-revisado: `f3401f4ce39fb21d6568028e2d24b50bec2c0cf2`.
Comparar `git diff f3401f4..HEAD`; o SHA final e o CI ficam na descrição da PR.

A re-revisão (18/10/2026, Linux x86_64, Docker 29.8.2, Compose v5.6.0, PostgreSQL
**16.15** real, Python 3.11.17) reproduziu os cinco comandos do relatório anterior no
SHA `f3401f4`: **436 passaram em 68,2 s**, **23 casos da revisão em 10,9 s**,
**59 observações e 2.000 contextos** iguais à Decision V3, **92 originais** OK; os dois
runs de CI do SHA também passaram. Os oito achados anteriores foram confirmados como
corrigidos, com probes próprios em PostgreSQL real para 409 QIT002010 (duas leases
liberadas, receipt 409, retry em 89 ms com fences 2/2), para dois documentos
concorrentes na mesma conta (201 e 409, nunca 503) e para o escape de U+2028/U+0085.
A rodada trouxe **quatro achados novos**, todos tratados nesta PR.

## Achados e decisões

| Achado | Prioridade | Estado | Correção e evidência |
|---|---|---|---|
| A — execução local terminal contradita pelo histórico bancário respondia 503 "repita o mesmo ID e corpo" (`selection.py:126` levantava `ProviderDataError`), enquanto a mesma contradição detectada em `_persist` já era 409 QIT002010 | Média | Corrigido | `reconcile_payments` levanta `ReconciliationRequired`; o conflito recebe **409 QIT002010** no enriquecimento, na releitura antes de persistir e sob os locks finais. Leases liberadas por owner/fence, hash preservado, sem espera de 10 s. `test_local_terminal_execution_contradicted_by_bank_history_is_409_not_retry_loop`: mesmo ID duas vezes e outro ID, três receipts 409, duas reservas liberadas, nenhuma análise nova. RED no SHA re-revisado: 503 em todos. |
| B — `test_pix_budget_uses_its_own_pool_and_preserves_base_wait` gravava Pix no banco compartilhado da sentinela (`pix_test_suite`) via `PixSessionLocal`, contra "bancos separados por caso" | Baixa | Corrigido | `database.pix_engine_for(url)` constrói o pool Pix; a aplicação continua ligando-o a `DATABASE_URL`, e o teste liga os mesmos parâmetros (checkout 100 ms, connect 1 s, 5 + 10, recycle, pre-ping) ao banco descartável do caso. O teste confirma que o ID não existe no banco compartilhado e existe no banco do caso, além da espera da 16ª solicitação Base. RED estrutural (fábrica inexistente), não um RED de comportamento de produção. |
| C — ramo `ReconciliationRequired('Conflicting bank execution identity')` em `_persist` sem regressão | Baixa | Corrigido | `test_conflicting_execution_identity_is_409_at_both_detection_points`: o fato local contraditório chega entre enriquecimento e `_persist` (409) e a requisição seguinte o encontra na primeira leitura (409). RED: a segunda resposta era 503. |
| D — `errors/handlers.py` registrava `request.url.path` e `exception.detail` sem escape; a correção do item 8 cobria só o request_logger | Baixa, pré-existente | Corrigido | `utils.logger.escape_for_log` (JSON, ASCII puro) usado pelo request_logger e pelos dois handlers. `tests/unit/test_pix_log_escape.py` exercita os handlers reais pelo ASGI com U+2028, NEL e CR/LF em path e detail. RED: separadores crus na linha de log. CR/LF só reproduz pelo `detail`, pois `Request.url` já os remove do path. |

## Refinamentos de contrato e limites

QIT002010 passa a cobrir também a contradição entre uma execução terminal local
(`failed`/`cancelled`, ingerida pela API interna da etapa 4) e o histórico bancário
que informa a mesma execução como `executed`. É conflito entre fatos da própria
instituição, não falha técnica; o retry não o resolve. Não se altera o motor, a
política, as migrations nem o contrato de eventos. Dados inválidos **dentro** da
própria resposta do provider (ID de execução reutilizado por operações distintas,
identidade de operação conflitante) continuam `ProviderDataError` e 503: são
material inválido do provider, não reconciliação de fato local.

O escape dos handlers é defensivo: as rotas Pix capturam suas exceções e não chegam
ao handler genérico; a mudança protege as rotas originais e qualquer rota futura.
A fábrica `pix_engine_for` não muda os parâmetros de produção; só os torna
vinculáveis a outra URL. O banco compartilhado da sentinela continua recebendo apenas
o registro de `sample_entity` da verificação inicial.

## TDD e validação

Primeira rodada, SHA re-revisado com os testes novos montados: **6 falhas** (A: 503
nos dois pontos; D: três separadores; B: fábrica ausente). Após as correções, a
rodada focada (`test_pix_api_rereview.py`, `test_pix_log_escape.py`,
`test_pix_api_review.py`, `test_pix_provider_selection.py`) passou com **56 testes em
11,99 s**. Build completa sem mounts de código, como o CI: **441 testes passaram em 74,02 s**
(436 anteriores + 5 novos), sem skips/xfails e somente o warning original do `declarative_base`.
Logs `pr6-rereview-{red,green-focused,full}.log` fora do Git. `git diff --check` e
compilação em memória de todos os arquivos Python conferidos. Testes verdes não
provam ausência de bugs.

Reprodução, na raiz deste Git, com Docker ativo:

```bash
docker compose -p pix-api-rereview -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-api-rereview -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/integration/test_pix_api_rereview.py tests/unit/test_pix_log_escape.py tests/integration/test_pix_api_review.py
docker compose -p pix-api-rereview -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-api-rereview -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python -m unittest discover -s /reference -p 'test_*.py' -q
docker compose -p pix-api-rereview -f compose.test.yml down -v --remove-orphans
```

O revisor autorizou corrigir e integrar nesta rodada; o merge depende do CI verde no
SHA final registrado na PR. A etapa 6 não recebe bug obrigatório conhecido desta etapa.
