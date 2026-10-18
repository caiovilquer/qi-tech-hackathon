# Evidências da PR 1

Registro da implementação funcional no commit `f5b02ca`. Posteriormente, as
fontes externas foram movidas para `docs/contexto/` a pedido do usuário.
O [índice atual](README.md) reúne todo o contexto disponível em um clone.

Validação local em 18/10/2026, macOS ARM64 com Docker Desktop. Containers:
Python 3.11, PostgreSQL **16.15** real. Os registros usados são sintéticos.
A confirmação da suíte Linux do GitHub fica nos checks da PR; os números
abaixo são execuções locais, não uma previsão de resultado do CI.

## Referência externa

Executado originalmente na pasta pai, sem alterar os arquivos de referência:

```bash
tmp/revisao-v2/.venv/bin/python -m unittest -q test_contrato_pix test_politica_pix
```

Após a organização das fontes, o comando equivalente na raiz do repositório é:

```bash
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
```

**92 passaram**, sem skips (36 contrato + 56 motor). O Python global não tinha
jsonschema; o ambiente de revisão já existente tem a versão 4.17.3 exigida.
O resultado de `scenarios()` foi serializado para JSON em memória e comparado
com `politica_simulacao_resultados.json`: **59 observações idênticas**. Não se
executou o entrypoint que sobrescreve esse arquivo. Não é evidência de HTTP,
banco, concorrência ou eficácia de detecção de fraude.

## Base antes das alterações

Commit `602d71c`: 67 arquivos conferidos byte a byte com o ZIP. Preservados
`.git`, origin e commit inicial `baffeee`. Nenhum Git foi criado na pasta pai.

A base original foi iniciada no projeto Docker dedicado `pix-baseline`, com
portas 3300/55432. Antes de mudar infraestrutura/helpers/documentação:

```bash
APP_ENV=testing SERVER_LOCALHOST=127.0.0.1 API_PORT=3300 \
DATABASE_URL=postgresql+psycopg2://bootcamp:bootcamp@127.0.0.1:55432/bootcamp \
.venv/bin/python -m pytest -q
```

**35 passaram em 3,27 s**, contra a API e PostgreSQL. Este comando é registro
histórico da base; a versão atual o recusa porque o banco não tem nome de testes.
Para reproduzir a suíte atual, usar a stack abaixo.

## Etapa implementada

```bash
docker compose -p pix-pr1-test -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
```

**57 passaram em 1,22 s**, exit code 0, após build com `requirements.lock` e
as mudanças finais nos helpers. Inclui os **35 originais sem alterar seus
arquivos de teste**, mais 21 testes/casos de migrations/guarda de reset e um
teste HTTP que consulta a linha gravada em uma conexão PostgreSQL independente.
Não há skips ou xfails. Tempo informado é do pytest, não do build/stack.

Verificações exercitadas pela suíte:

- Schema vazio, reexecução sem efeito e upgrade preservando dados.
- Adoção explícita da base existente preservando entidade e sequência.
- Recusa de adoção sem opção explícita ou com schema parcial/divergente:
  nulabilidade, tipo, default, FK, unique e status inicial.
- Rollback de DDL e histórico de todo o lote em falha, tanto vazio como upgrade.
- Recusa de SQL aplicado alterado e versão aplicada desconhecida.
- Dois migradores simultâneos: execução única de DDL/dados/histórico.
- Sequência inválida de arquivos e recusa de resets fora do ambiente dedicado.
- HTTP da base: criação, consulta, alteração, paginação, validação,
  autenticação, erros e persistência confirmada em conexão independente.

O teste de concorrência usa duas conexões PostgreSQL e uma barreira; não é uma
simulação sequencial. Ele verifica **migrations**, não concorrência Pix.

## Smoke do ambiente de uso e recuperação local

```bash
API_PORT=3301 DB_PORT=55433 docker compose -p pix-pr1 up -d --build --wait
```

- PostgreSQL pronto → migration concluída com 0 → API saudável.
- `/health_check` retornou 204; rota interna sem token retornou 403.
- O exemplo de cadastro do README retornou 201; GET retornou 200.
- Reinício da API, seguido de GET: DTO exatamente igual ao previamente salvo.
- `docker compose -p pix-pr1 down` **sem -v**, seguido de `up -d --wait`:
  containers recriados, mesma consulta/DTO preservados pelo volume nomeado.
- Usuário da API no container: UID 100 (não root).
- Banco original usado no baseline: adoção real executada pelo runner,
  **28 entidades sintéticas preservadas**; segunda execução sem migrations
  pendentes. Além dessa prova manual, a adoção é coberta pela suíte automatizada.

A execução do smoke detectou um exemplo inicial de CPF sem máscara, recusado
pelo schema da entidade de exemplo. O README foi corrigido e seu exemplo com
máscara foi executado com sucesso. Isso não altera o contrato Pix futuro.

## Revisão própria e integridade

- `git diff --check` e compilação Python dos arquivos novos/helpers.
- `cmp database/database.sql migrations/0001_base.sql`: iguais.
- `git diff 602d71c -- src tests/integration/sample_entity tests/integration/test_healthcheck.py`:
  sem alterações. Camadas, middleware e os testes originais preservados.
- Execução de pytest sem ambiente dedicado: recusada no início, exit code 4,
  antes de qualquer teste/reset.
- Revisados runner, testes, Compose, Dockerfile, CI, lock e documentação.
- Corrigidos durante revisão própria: dependências transitivas abertas,
  instruções antigas de reset, exemplo de CPF e tratamento genérico/placeholder
  do helper de resposta de testes. Sem pendência conhecida dentro desta etapa.
- Temporários/logs/venv não fazem parte dos commits. As fontes originalmente
  externas foram incluídas depois em `docs/contexto/`, preservadas byte a byte.
  Somente credenciais locais fictícias nos exemplos/Compose.

## Limites e próximos gates

A PR 1 não entrega motor, contrato HTTP Pix, tenancy, idempotência Pix, eventos,
claims/leases/fencing, DICT ou locks de agregados. Esses componentes pertencem
às etapas previstas no plano e não foram iniciados aqui. O exemplo da base
não é cadastro bancário nem motor antifraude. Liveness não mede prontidão do DB.

O runner é forward-only e aceita SQL confiável transacional, sem downgrade
ou reparo de schema. Adoção confere a estrutura inicial, não faz auditoria
universal de triggers/permissões. Imagens mantêm tags de série; dependências
Python estão fixadas, mas não se promete um build hermético de binários.
Nenhum deploy, integração bancária real ou merge foi executado.

## Atualização de contexto para a revisão

As 24 fontes foram movidas para `docs/contexto/` com conferência de SHA-256
antes/depois; o prompt foi movido para `docs/REVISAO-PR1.md` e atualizado.
Os links locais do guia V3, revisão independente, índice, inventário, README,
plano e evidências foram conferidos. Arquivos históricos permanecem com os
bytes recebidos e estão identificados como histórico no índice.

Após a movimentação: **92 testes da referência passaram**, sem skips; as
**59 observações** foram reproduzidas sem sobrescrever o JSON. O novo build
Docker e a suíte da API também passaram: **57 testes**, PostgreSQL real,
exit code 0. `pytest.ini` separa a descoberta da API (`tests/`) das fontes
Python arquivadas; `.dockerignore` exclui `docs/contexto/` da imagem.
Nenhuma implementação das próximas etapas foi antecipada.
