# Evidências e TDD — etapa 5

Base conferida: `origin/main` **3daa579ef040e49f3f242a1bf7b39d01f9aefabc**, merge
da PR #5 (etapa 4). Checkout limpo, `git fetch origin --prune`, `git switch main`,
`git pull --ff-only origin main` e `git switch -c feat/pix-api`. Origin SSH existente
`git@github.com:caiovilquer/qi-tech-hackathon.git` preservado; sem Git na pasta pai.

## Baseline e sequência TDD

Em 18/10/2026, baseline Docker/PostgreSQL real: **366 passaram em 8,06 s**.
Os testes HTTP foram escritos antes do novo coordenador: primeiro RED por
`ModuleNotFoundError: controllers.pix_api`, não por asserts de comportamento.
Os primeiros 15 casos novos passaram após integrar as camadas. Uma guarda antiga
que afirmava ausência da rota Pix foi atualizada para exigir POST/GET nesta etapa;
a verificação de preservação de sample entity e política no startup permaneceu.

Testes de regressão da revisão própria registraram RED antes das correções:

- Vínculo bancário criado entre enriquecimento e locks: uma execução contava duas
  vezes como pagamento maduro. Nova reconciliação sob locks usa fatos locais atuais.
- Fatos importados só tinham referências na auditoria. Pagamentos completos agora
  ficam congelados no snapshot privado, sem criar tentativas API fictícias.
- Conteúdo armazenado corrupto era traduzido em GET 404. Falha de leitura/codec é
  503; validação separada identifica IDs que não poderiam existir e responde 404.
- JSON de tenants duplicado era aceito; raízes lista/null escapavam como
  AttributeError. Configuração agora exige objeto, chaves únicas e credencial/ISPB
  válidos vinculados a políticas existentes.
- Corpo chunked incompleto esgotava todo o prazo sem finalizar o receipt de erro;
  replay respondia 200 mesmo se a finalização de receipt falhasse. Erros de corpo
  reservam o encerramento dentro dos 3 s; finalização de replay é obrigatória.
- Erros semânticos deixavam de associar um ID extraível ao receipt. O parser
  agora preserva esse ID sem reservar uma operação inválida nem inventar hash.
- Execução bancária com conhecimento futuro ganhava recebimento local antecipado.
  Persistência e revalidação filtram ocorrência/conhecimento no instante adequado.

Uma expectativa de teste foi corrigida: revogação impõe rejeição com zero pontos
no motor e, sozinha, não reduz `C_pre`. O cenário de reconsulta usa também uma
recuperação recente (+25), sem alterar a política para satisfazer o teste.
Uma execução intermediária montava `/app` mas o pytest preferia `/workspace/src`
da imagem antiga; seus resultados não foram usados como prova das correções.
A validação final usa build completo, sem mounts de código, como o CI.

Outros testes começaram verdes porque documentavam comportamento já correto:
rollback de erro SQL em flush e trigger deferred realmente falhando no COMMIT,
confirmação perdida após commit real, crash de processo, source check durável,
concorrência/fan-in, segurança vencida, provider lento/indisponível e isolamento.
Não se afirma que todo teste novo correspondia a um bug.

## Cobertura da integração

| Garantia | Evidência |
|---|---|
| HTTP oficial e centavos exatos | Três status, topo exclusivo, consulta por ID, parser de bytes, decimal fracionário que float arredondaria, JSON duplicado/NaN, erros 400/422/413, limite chunked/exato |
| Idempotência/tenant | Quatro POSTs simultâneos: um 201 e três 200 com DTO igual; hash diferente 409 sem esperar dono; mesma chave em outro tenant independente; replay sem providers |
| Origem e destino | IDs distintos da mesma origem contam uma vez cada; cinco origens concorrentes ao mesmo destino observam fan-in 1..5 e ativam MULE_FAN_IN |
| Inicial durável | Falha posterior do destinatário não apaga conclusão do remetente; próximo ID não repete sua consulta inicial |
| Revalidação | Revogação/cache mais novo, `C_pre` exigindo consulta, novo vínculo ainda sem ingestão e limite de três ciclos com segurança vencida |
| Temporalidade/reconciliação | Vínculo criado durante a requisição não duplica execução; conhecimento futuro não vira evento já conhecido; importações completas no snapshot, sem novas tentativas |
| Eventos concorrentes | HTTP pausado no provider enquanto outra thread ingere falha terminal de 2FA; locks finais observam liberação exata da exposição e preservam análise original |
| SQL/commit | SQL real falhando no flush; constraint trigger deferred falhando no commit; outra unique 23505 não é replay; perda da confirmação após commit real resolve pelo retry 200 |
| Crash/reinício | Subprocesso Uvicorn morto durante provider; ambas as reservas sobrevivem; outro processo retoma com fences 2; terceiro processo devolve DTO original por POST/GET |
| Deadline | `pg_sleep(5)` cancelado sem commit tardio, provider cancelável com oito tentativas de até 250 ms, health paralelo sem bloquear event loop, corpo incompleto e claim ocupado com 503 limitado |
| Banco indisponível | Conexão PostgreSQL real recusada: POST/GET 503; nenhum receipt inventado; autenticação inválida preserva 403 |

O teste de crash expira as leases por SQL no banco descartável para não esperar
10 s por cenário; não altera a duração de produção. A perda de confirmação é
injetada **depois de um commit real**, não por corte de rede externo. Os testes
HTTP sobem servidores reais em portas aleatórias, com bancos separados por caso;
a sentinela existente protege a stack antes da coleta. Não há SQLite ou repository
fake validando persistência. Providers adversariais são objetos explícitos de teste
com os mesmos protocolos; a aplicação carrega adapters de fixtures normalizadas.

## Resultado final local

Em **18/10/2026**, build completa sem mounts de código na stack `pix-pr5-submit`:
**413 testes passaram em 31,18 s**, sem skips/xfails: 366 anteriores e **47 novos**
(39 HTTP/PostgreSQL e 8 de configuração). Um único warning, a depreciação do
`declarative_base` original da base. Ambiente: macOS ARM64, Docker **29.8.2**,
Compose **5.6.0**, Python **3.11.17** e PostgreSQL **16.15** real.

- A suíte focada anterior passou com **47 casos em 23,27 s**; o último ajuste de
  ID em receipt inválido foi verificado na suíte completa final acima.
- **92 testes originais** passaram em **0,133 s**, fontes montadas somente para
  leitura; **59 observações e 2.000 contextos** seguem iguais à Decision V3 completa.
- Smoke contra a **imagem da API** no Compose, usando o exemplo do README:
  approved, two_factor_required e rejected retornaram 201; cada POST repetido e
  GET retornou 200 com DTO idêntico. O caso de rejeição usa o limite noturno;
  não se presume que um valor alto diurno, sozinho, resulte rejeitado na V3.
- **100 arquivos Python** compilados em memória, sem gerar bytecode no host;
  revisão do diff, `git diff --check` e **24 hashes** das fontes conferidos.
  Nenhuma migration/schema/fonte de contexto foi alterada.
- Stacks descartáveis desta rodada removidas com volumes/órfãos ao concluir;
  ambiente de uso `pix-pr1` preservado. Sem deploy ou merge da etapa 5.

Logs: `pr5-baseline.log`, `pr5-red.log`, `pr5-revalidation-red.log`,
`pr5-config-red.log`, `pr5-body-red.log`, `pr5-future-red.log`,
`pr5-get-id-red.log`, `pr5-invalid-receipt-red.log`, `pr5-submit-full.log`,
`pr5-ready-focused.log`, `pr5-ready-reference.log`, `pr5-ready-original.log`,
`pr5-submit-smoke.log` e `pr5-syntax.log`, todos em `../tmp/`, fora do Git.
O SHA e os links de CI da revisão ficam na descrição da PR; resultados locais
não substituem a revisão independente nem provam ausência de bugs.

## Reprodução

Na raiz deste Git, com Docker e Compose v2:

```bash
docker compose -p pix-api-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-api-review -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/integration/test_pix_api.py tests/unit/test_pix_http_configuration.py
docker compose -p pix-api-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-api-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python -m unittest discover -s /reference -p 'test_*.py' -q
docker compose -p pix-api-review -f compose.test.yml down -v --remove-orphans
```

Usar nome exclusivo por execução concorrente; não executar testes destrutivos no
ambiente de aplicação. Logs desta rodada ficam em `../tmp/pr5-*.log`, fora do Git.

## Limites

Sem deploy, merge automático, DICT/banco remoto, execução financeira, criação de
2FA ou endpoint público de eventos. O protótipo não mede eficácia da política.
O prazo é operacional/cooperativo, não garantia de tempo real com processo/host
suspenso; um commit já enviado pode ter resultado desconhecido. A etapa 6 amplia
aceitação/demonstração e organiza a documentação final, sem adiar bugs obrigatórios.
