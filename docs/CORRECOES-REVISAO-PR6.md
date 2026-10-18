# Correções da revisão independente — etapa 6

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/7
Branch: `test/pix-delivery`. SHA revisado: `5d231532ac47c057bb03543f2c9b3f64cf6baef1`.
Comparar `git diff 5d23153..HEAD`; o SHA final e o CI ficam na descrição da PR.
Revisão registrada no GitHub:
https://github.com/caiovilquer/qi-tech-hackathon/pull/7#pullrequestreview-5562930607

A revisão (18/10/2026, Linux x86_64, Docker 29.8.2, Compose v5.6.0 com builder
clássico, PostgreSQL 16 e Python 3.11 nas imagens) reproduziu os comandos do
relatório no SHA `5d23153`: **445 passaram em 84,70 s**, **59 observações e 2.000
contextos** iguais à Decision V3, **92 originais** OK, demo com exit 0 no projeto da
suíte e em projeto novo, com JSON idêntico; os dois runs de CI do SHA também
passaram. Sem regressão, falha de segurança ou mudança de contrato, política,
migrations ou dependências. A rodada trouxe **cinco achados**, todos tratados aqui.
As sondas da revisão rodaram na imagem de testes, fora do conftest, com
`run --rm --no-deps -v <dir>:/probes:ro -e PYTHONPATH=/workspace:/opt/pix tests
python /probes/<sonda>.py`.

## Achados e decisões

| Achado | Prioridade | Estado | Correção e evidência |
|---|---|---|---|
| 1 — a demo não exercitava "execução recém-conhecida não amadurece retroativamente": `after-execution` era datado `base + 1 min`, então `maturity = t − 24h` ficava antes do `transaction_at` de `two-fa`; o primeiro termo de `features.py:153` já era falso e o `occurred_at` da execução nunca era avaliado. Sonda: com o Pix seguinte a +25h, `mature=0` com execução agora e `mature=1` com execução no passado; a +1 min, `mature=0` nos dois casos | Média | Corrigido | `after-execution` passa a `base + 25h` (ainda no passado). O runner exige `payment_older_than_maturity_window` verdadeiro, mantém `mature_payments == 0` e fixa o status seguinte em `two_factor_required` (aparelho não cadastrado +20, destinatário não coberto +10, sob incerteza), antes aceito como `approved` ou `two_factor_required` sem registro. O relatório ganha `payment_older_than_maturity_window` e `follow_up_status`. RED: `test_delivery_scenarios_survive_process_restart_and_leave_no_database` falhou por faltarem as duas chaves. A propriedade do motor já era coberta por `tests/unit/test_pix_policy_reference.py::test_profile_maturity_requires_execution_to_be_twentyfour_hours_old`; a demo agora a demonstra |
| 2 — sem handler de sinal: SIGTERM (`docker stop`, encerramento do Compose) ou SIGKILL (timeout do `subprocess.run` no teste) encerravam o runner sem executar os `finally`. Sonda: banco `pix_test_demo_*` restante e Uvicorn órfão com PPID 1, returncode -15/-9 | Baixa | Corrigido | `main()` registra `signal.signal(SIGTERM, …)` que levanta `SystemExit(143)`, de modo que `api_process` encerra o filho e `disposable_database` remove o banco. O teste cria o runner em sessão própria (`start_new_session=True`) e, em timeout, envia SIGTERM/SIGKILL ao grupo. `test_sigterm_during_live_api_stops_uvicorn_and_drops_database`: espera a primeira análise ser gravada no banco da demo, envia SIGTERM e exige exit 143, mesmo conjunto de bancos e grupo de processos vazio. RED: `-15 == 143` falhou. SIGKILL continua sem limpeza e documentado como tal |
| 3 — evidência do cenário adverso incompleta: a saída real (CI e local) é `PAYEE_DICT_ADVERSE`, `PAYEE_DICT_RESTRICTED` e `DICT_CHECK_UNAVAILABLE` (remetente não coberto), e a matriz listava "contagem de fraude sozinha não é rotulada como restrição" como resultado obrigatório da demo, embora a fixture tenha contagem e restrição juntas e o runner só exigisse `PAYEE_DICT_RESTRICTED` | Baixa | Corrigido | `validacao-pr6.md` e `aceitacao-pix.md` registram os três motivos; a distinção contagem × restrição passa a apontar para `tests/unit/test_pix_policy_reference.py::test_adverse_counters_do_not_imply_explicit_restriction`, que já existia. O teste de aceitação fixa a lista exata de motivos observados. Não se acrescentou cenário à demo para preservar as 21 análises e 6 checagens documentadas |
| 4 — falha de startup da API filha sem diagnóstico: stdout/stderr do Uvicorn iam para um `TemporaryFile` nunca lido; com fixture inválida a mensagem era só `Demo API failed to start` | Baixa | Corrigido | `tail(logs)` lê o final do arquivo com `os.pread`, que não move o offset compartilhado com o filho, e a mensagem passa a incluir o traceback. `test_api_startup_failure_reports_child_output`: fixture `{"unexpected": []}` deve produzir erro contendo `Unknown fixture dataset fields`. RED: regex não casou com a mensagem curta |
| 5 — limpeza com Uvicorn vivo não testada: `test_demo_removes_only_its_database_when_a_scenario_fails` força a falha antes de existir processo ou conexão | Baixa, teste ausente | Corrigido | `test_scenario_failure_with_live_api_stops_uvicorn_and_drops_database` substitui `post` para falhar na sexta chamada HTTP, com a API filha servindo e o pool aberto, e exige saída não zero, mesmo conjunto de bancos e grupo de processos vazio. Começou verde: cobre o `finally` já existente, como a sonda da revisão havia mostrado |

## Refinamentos e limites

Mudar a data do Pix seguinte não altera contagens: continuam 21 análises/Pix e 6
checagens iniciais, 1 evento de autenticação e 1 de execução, e o replay após
reinício permanece idêntico. A escolha de 25h, e não 24h exatas, evita depender da
borda inclusiva da janela; a borda é coberta pelo motor puro.

O status `two_factor_required` do Pix seguinte decorre de `UNREGISTERED_DEVICE`
(+20) e `DICT_CHECK_UNAVAILABLE` (+10) com incerteza de perfil e destinatário; não
há `DEVICE_POLICY_LIMIT` (5.000 abaixo do corte e exposição diária do aparelho no
dia local anterior). Nenhum peso, corte, janela ou migration foi alterado.

O tratamento de SIGTERM cobre um sinal; um segundo SIGTERM durante a limpeza pode
interrompê-la, e SIGKILL não executa nada. Nos dois casos o `down -v` da stack
descartável remove o servidor inteiro. O handler existe só no `main()` do runner,
não na aplicação.

## TDD e execução

```bash
docker compose -p pix-pr7fix -f compose.test.yml up -d --build --wait api
docker compose -p pix-pr7fix -f compose.test.yml run --rm --no-deps \
  -v "$PWD/scripts:/workspace/scripts:ro" -v "$PWD/tests:/workspace/tests:ro" tests \
  python -m pytest -q -p no:cacheprovider tests/integration/test_pix_delivery.py
```

1. Testes escritos antes das correções, sobre o runner do SHA revisado:
   **3 failed, 4 passed em 15,82 s**. Falharam o relatório sem as chaves novas, o
   SIGTERM com `-15` e a mensagem de startup sem causa; o caso de falha com API viva
   nasceu verde, como registrado acima.
2. Correções em `scripts/demo_pix.py`: **7 passed em 20,60 s** no mesmo comando,
   após ajustar o teste de startup para regex multilinha (`(?s)`).
3. Build completa sem mounts e demo em projeto novo: ver a seção abaixo e
   [validacao-pr6.md](validacao-pr6.md).

## Suíte integral e demo após as correções

Build completa sem mounts em projeto novo (`pix-pr7full`): **448 passed, 1 warning
em 86,82 s**, exit 0, sem skips/xfails, os 445 casos submetidos mais 3 da revisão.
Demo em projeto novo (`pix-pr7demo`, `run --build --rm demo`): exit 0 em 17 s
incluindo build e containers, mesmas 21 análises e 6 checagens, replay idêntico,
`payment_older_than_maturity_window: true`, `follow_up_status: two_factor_required`
e `mature_after_execution: 0`. Projetos removidos com `down -v --remove-orphans`.
O CI do SHA final consta na descrição da PR.
