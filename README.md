# Antifraude Pix

Implementação em etapas sobre a Base API do Bootcamp QI Tech. **Etapa atual:
base reproduzível e migrations, em revisão.** O motor e os endpoints Pix ainda
não estão disponíveis. A API de exemplo da base funciona com PostgreSQL real.
O [plano de implementação](PLANO-IMPLEMENTACAO.md) define as próximas PRs e os
critérios de aceitação. Cada etapa depende de revisão e aprovação da anterior.

Para revisão independente, começar pelo [índice de documentos](docs/README.md)
e pelo [prompt da PR 1](docs/REVISAO-PR1.md). Todas as fontes fornecidas estão
versionadas em `docs/contexto/`, inclusive guia vigente, enunciado, ZIP,
referência executável e histórico identificado.

## O que esta etapa entrega

- Base fornecida importada integralmente, conferida contra o ZIP, em commit
  separado `602d71c`. [Inventário e hashes das fontes](docs/fontes.md).
- Mesmas camadas Resource → Controller → Repository/Model → DTO,
  contexto por requisição, `INTERNAL-TOKEN`, logger e session manager.
- Migrations SQL versionadas, com checksum, transação e lock PostgreSQL;
  inicialização de banco vazio e adoção validada de banco da base existente.
- Compose com PostgreSQL 16, volume nomeado e migration antes da API;
  imagem Python 3.11 executada sem root e dependências transitivas fixadas.
- Stack de testes separada, descartável, sem portas publicadas, com testes
  HTTP e PostgreSQL. O CI executa essa mesma stack em Linux.

Não há análise Pix, idempotência Pix, integração DICT, eventos bancários ou
regra antifraude nesta etapa. As rotas de exemplo não fazem parte do contrato
Pix. O health check mede liveness da API e não confirma disponibilidade do banco.

## Como rodar

Requer Docker com Compose v2 e daemon iniciado. Na raiz **deste repositório**:

```bash
docker compose up -d --build --wait
curl -i http://127.0.0.1:3000/health_check
```

Resultado esperado: `204`, sem corpo. `/` retorna nome do serviço e PID.
Os serviços só publicam portas em `127.0.0.1`. Banco e API usam 5432 e 3000;
para escolher outras portas:

```bash
API_PORT=3300 DB_PORT=55432 docker compose up -d --build --wait
```

O `.env` é opcional. [.env.example](.env.example) contém apenas valores locais
fictícios. Nunca usar esses valores como credenciais de um ambiente real.

Exemplo da base (não é criação de conta bancária nem análise de Pix):

```bash
curl -i http://127.0.0.1:3000/sample_entity \
  -H 'INTERNAL-TOKEN: default_token' -H 'Content-Type: application/json' \
  -d '{"hello":"world","name":"Pessoa Sintetica","email":"demo@example.test","document_number":"123.456.789-09","birthdate":"1990-01-01"}'
# 201: copiar sample_entity_key para consultar abaixo
curl -i http://127.0.0.1:3000/sample_entity/CHAVE_RETORNADA \
  -H 'INTERNAL-TOKEN: default_token'
```

Documento válido matematicamente não comprova identidade. Repetir o cadastro
com mesmo documento/e-mail retorna conflito, como na base. Sem token, as rotas
internas retornam `403 QIT000002`. As rotas Pix ainda retornam 404 com token.

```bash
docker compose logs migrate api
docker compose restart api
docker compose down
```

O volume `postgres_data` preserva dados após restart e `down`. **Não usar
`down -v` no ambiente de uso:** esse comando remove o volume. Dados de volumes
anônimos da base antiga não são automaticamente transferidos ao novo volume;
ver [migrations e adoção](docs/migrations.md) antes de trocar um ambiente existente.

## Como testar

Os testes da base derrubam o schema `public`. Por isso a suíte usa **um projeto
Compose separado**, com banco `pix_test_suite` em tmpfs e sem portas publicadas:

```bash
docker compose -p pix-tests -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
# Remove somente a stack de testes:
docker compose -p pix-tests -f compose.test.yml down -v --remove-orphans
```

O primeiro comando devolve o exit code do pytest. Não executar dois testes
sobre a mesma stack ao mesmo tempo; cada execução concorrente precisa de outro
nome em `-p`. Migrations são exercitadas em bancos aleatórios adicionais,
criados e removidos no servidor de testes (o usuário local de testes precisa
poder criar bancos). A API não ganha endpoints administrativos para isso.

Execução local da etapa: **57 testes passaram**, incluindo os 35 testes
originais. A referência externa tem **92 testes** e **59 observações** da
simulação reproduzidos separadamente; esses números não são testes HTTP/Pix.
Ver comandos, resultados e limites em [docs/validacao-pr1.md](docs/validacao-pr1.md).

Para um ambiente Python local opcional:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -c requirements.lock
```

A suíte recusa `APP_ENV` diferente de `testing` e nomes de banco sem prefixo
`pix_test_`. Não apontar os testes para o banco do ambiente de uso. O caminho
recomendado e executado é o Compose isolado acima; não exige Python no host.

## Migrations

[Procedimento completo](docs/migrations.md). A migration inicial reproduz
`database/database.sql`. Esse arquivo e o Dockerfile antigo do banco foram
preservados como referência, mas o Compose atual não os usa como initializer.
A fonte de evolução passa a ser `migrations/NNNN_descricao.sql`.

```bash
docker compose run --rm migrate
# Em banco existente fora do Compose, com as dependências instaladas:
# export DATABASE_URL=<URL do banco correto>
.venv/bin/python -m migrations --adopt-base
```

Nunca editar uma migration aplicada; adicionar a próxima. Não há downgrade
automático. Mudanças futuras precisam de migration compatível e teste de
upgrade preservando dados. Falhas impedem a API de iniciar via Compose.

## Como o motor de decisão funcionará

O enunciado pede entrada oficial e saídas `approved`, `rejected` ou
`two_factor_required`, com topo `key`, `status`, `metadata` e consulta por ID.
O guia V3 fixa nossa política **experimental** `pix-experimental-v3`, que será
portada para um motor puro antes de expor os endpoints. Score, cortes, janelas
e TTL não são probabilidades calibradas ou prova de conformidade regulatória.

Histórico de execução, resultado de 2FA e análise serão fatos separados;
replay/GET preservarão a análise original. O simulador Python não será usado
como banco nem apresentado como evidência de transações ou concorrência.
Detalhes e dependências estão no plano; nenhuma regra é declarada ativa aqui.

## Desenvolvimento e limites

- Git, branches e commits ficam somente nesta subpasta. Não inicializar Git na
  pasta pai. O ZIP original fornecido é a exceção documental versionada em
  `docs/contexto/`; não adicionar outros arquivos compactados, ambientes locais,
  caches, logs ou temporários.
- Nenhum deploy, merge automático ou integração com banco/DICT real nesta etapa.
- `requirements.txt` e `requirements-dev.txt` mantêm as versões diretas da base.
  `requirements.lock` fixa também as transitivas para Python 3.11/Linux,
  usado no Docker/CI. Para atualizar de forma deliberada, gerar com
  `uv pip compile --python-version 3.11 --python-platform linux requirements-dev.txt -o requirements.lock`
  e revisar/testar as mudanças. Imagens usam tags de série Python/PostgreSQL;
  o build não é uma distribuição hermética de binários.
- [Organização original da base](docs/como-o-projeto-e-organizado.md) explica
  as camadas. Para execução e reset de testes, este README prevalece.
