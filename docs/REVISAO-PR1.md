# Prompt para revisão independente da PR 1

Revise a implementação da primeira etapa da API Antifraude Pix. Procure bugs,
regressões, falhas de segurança, inconsistências e testes ausentes. Não assuma
que testes passando demonstram ausência de bugs; confira o código e reproduza
os problemas que encontrar.

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/1
Branch: feat/pix-foundation
Base: main (baffeeead1f541d8d91ed3762a6a8c595d7f5bd9)
SHA da implementação funcional: f5b02cae18fb4b914ca4b98886f7c8fb3360d5cf
A PR contém também um commit posterior de organização documental. Revise o
HEAD atual da PR, cujo SHA exato está na descrição da PR. Para obtê-lo:
`gh pr view 1 --repo caiovilquer/qi-tech-hackathon --json headRefOid --jq .headRefOid`.
Registre o SHA efetivamente revisado no relatório.
Repositório local: /Users/caiovilquer/Developer/personal/qi-tech-hacka/bootcamp-base-api

## Objetivo e limites

Esta etapa entrega a base obrigatória importada, ambiente reproduzível,
migrations iniciais, adoção verificada de banco da base existente e testes
HTTP/PostgreSQL do exemplo. Não entrega motor ou rotas Pix, tenancy, DICT,
eventos bancários ou idempotência/concorrência Pix. Esses itens estão nas
próximas etapas, que ainda não foram implementadas. Verifique se o código e a
documentação respeitam esses limites sem apresentar comportamento incompleto
como pronto.

O commit 602d71c importa 67 arquivos byte a byte iguais ao ZIP original.
408710b contém o plano. f5b02ca contém a implementação da etapa. Para focar a
revisão: git diff 602d71c..f5b02ca. src/ e os 35 testes HTTP originais não foram
alterados; os helpers de teste foram adaptados. Para as mudanças de contexto
posteriores, confira também `git diff f5b02ca..HEAD`: fontes arquivadas,
índice/prompt/links e separação da descoberta de testes/build.

## Requisitos e critérios de aceitação

- Preservar Base API: Resource → Controller → Repository/Model → DTO, contexto,
  autenticação INTERNAL-TOKEN, logger e session manager.
- Inicializar PostgreSQL vazio por migration explícita e versionada.
- Reexecução sem efeito; checksum impede edição de SQL aplicado; versões
  desconhecidas/fora de ordem são recusadas.
- DDL e histórico são atômicos; falha faz rollback; migradores concorrentes não
  duplicam execução. Avaliar lock/timeout/transação também em falha.
- Adotar base legada apenas com validação estrutural; recusar schema parcial
  ou divergente; preservar linhas, IDs e sequências. Conferir search_path,
  defaults, constraints e limpeza do schema de referência.
- Compose inicia API após migration bem sucedida; volume preserva dados;
  usuário da API sem root; stack de testes separada e descartável.
- Resets destrutivos limitados a APP_ENV=testing e banco pix_test_; não usar
  banco do ambiente de uso. Não publicar segredos ou temporários.
- Health 204, autenticação/erros e CRUD do exemplo preservados; escrita HTTP
  confirmada no PostgreSQL real, consulta preservada após restart.
- README e evidências reproduzíveis e honestas sobre o que foi executado.

## Arquivos relevantes

PLANO-IMPLEMENTACAO.md, README.md, docs/README.md, docs/REVISAO-PR1.md,
docs/contexto/, docs/fontes.md, docs/migrations.md, pytest.ini, .dockerignore,
docs/validacao-pr1.md, migrations/runner.py, migrations/__main__.py,
migrations/0001_base.sql, database/database.sql, Dockerfile,
docker-compose.yml, compose.test.yml, requirements.lock,
.github/workflows/tests.yml, tests/conftest.py, tests/utils/db_utils.py,
tests/utils/requisition.py, tests/integration/test_migrations.py,
tests/integration/test_base_persistence.py e testes originais da base.

Todas as fontes fornecidas estão versionadas em docs/contexto/, inclusive
GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md (V3), REVISAO-INDEPENDENTE-GUIA.md,
Hackathon Antifraude Pix.pdf (páginas 19–25), bootcamp-base-api.zip e os
scripts/testes de referência. Comece por docs/README.md; hashes em
docs/fontes.md. Os documentos históricos também foram preservados e
identificados no índice; não os use como especificação vigente.
Para esta etapa, plano/README delimitam o escopo; o guia completo descreve
obrigações de etapas futuras. Parâmetros experimentais não são requisitos
oficiais nem evidência de conformidade.

## Reprodução

Na raiz do repositório, com Docker/Compose v2 e daemon disponível:

```bash
git fetch origin
git switch --detach origin/feat/pix-foundation
git rev-parse HEAD
# Conferir com o headRefOid atual da PR e registrar o SHA revisado
docker compose -p pix-independent-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-independent-review -f compose.test.yml down -v --remove-orphans
API_PORT=3302 DB_PORT=55434 docker compose -p pix-independent-app up -d --build --wait
curl -i http://127.0.0.1:3302/health_check
```

Use projetos/portas livres. A suíte cria bancos adicionais no servidor de teste
para casos de migration. Não execute resets no banco da aplicação. Para o
smoke de persistência, use o cadastro sintético do README, consulte pela chave,
reinicie a API e repita o GET; down sem -v seguido de up deve preservar os dados.
O cleanup com -v acima se refere somente à stack descartável de testes.

Resultados locais registrados: 35 testes da base antes das mudanças; 57 após
implementação; adoção manual com 28 registros sintéticos preservados;
consulta igual após restart da API e recriação dos containers. A referência
externa passou em 92 testes e reproduziu 59 observações, sem comprovar API/Pix.
Os checks da implementação funcional concluíram com sucesso, com 57 testes em Linux/PostgreSQL. Confira também os checks do HEAD atual e faça sua própria revisão; isso não exclui bugs.

A referência documental pode ser executada separadamente, com as dependências
instaladas: `.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q`.
O pytest padrão fica limitado a `tests/`; não confunda a inclusão das fontes
em docs com implementação antecipada das próximas etapas.

## Formato dos achados

Para cada achado acionável: prioridade, arquivo e linha, requisito/garantia
violado, condição que dispara o bug, reprodução ou evidência, impacto e
correção sugerida. Distinga defeitos desta etapa de funcionalidades
explicitamente futuras e melhorias opcionais. Aponte incertezas ou testes que
não conseguiu executar. Não altere o código, faça merge ou avance a próxima
etapa durante a revisão. Se não encontrar achados, diga isso e registre os
limites da análise.
