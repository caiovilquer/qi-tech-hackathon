# Antifraude Pix

Implementação em etapas sobre a Base API do Bootcamp QI Tech. **Estado atual:
PRs 1, 2 e 3 aprovadas e mergeadas; etapa 4 de persistência interna
(`feat/pix-storage`) implementada, aguardando revisão.** A API de exemplo funciona com
PostgreSQL real; os endpoints Pix ainda não estão disponíveis.
O [plano de implementação](PLANO-IMPLEMENTACAO.md) define as próximas PRs e os
critérios de aceitação. Cada etapa depende de revisão e aprovação da anterior.

Para revisão independente do que já existe, começar pelo
[índice de documentos](docs/README.md), que aponta prompt, correções e
evidências de cada PR. Todas as fontes fornecidas estão
versionadas em `docs/contexto/`, inclusive guia vigente, enunciado, ZIP,
referência executável e histórico identificado.

## O que já foi entregue

- Base fornecida importada integralmente, conferida contra o ZIP, em commit
  separado `602d71c`. [Inventário e hashes das fontes](docs/fontes.md).
- Mesmas camadas Resource → Controller → Repository/Model → DTO,
  contexto por requisição, `INTERNAL-TOKEN`, logger e session manager.
  A conexão da API fixa `search_path=public`, como as migrations.
- Migrations SQL versionadas, com checksum, transação e lock PostgreSQL;
  inicialização de banco vazio e adoção validada de banco da base existente.
- Compose com PostgreSQL 16, volume nomeado e migration antes da API;
  imagem Python 3.11 executada sem root e dependências transitivas fixadas.
- Stack de testes separada, descartável, sem portas publicadas, com testes
  HTTP e PostgreSQL. O CI executa essa mesma stack em Linux.

A PR 2 acrescenta parsing JSON exato, normalização/hash do contrato oficial,
25 critérios puros, políticas imutáveis por tenant/versão validadas no startup
e resposta pública separada do snapshot de auditoria. Ver
[contrato e motor](docs/motor-pix.md) e [evidências](docs/validacao-pr2.md).

A PR 3 acrescenta contratos e adapters simulados em `src/providers/`,
validação de escopo/versão/validade, DICT condicional, orçamento de chamadas e
reconciliação de execuções bancárias sem fabricar solicitações da API.
[Interfaces e limites](docs/providers-pix.md), [evidências/TDD](docs/validacao-pr3.md)
e [correções da re-revisão](docs/CORRECOES-REREVISAO-PR3.md).

Há persistência e idempotência internas Pix, além de ingestão de eventos
bancários vinculados. As rotas Pix e a integração completa com os providers
ficam na etapa 5. Não há integração externa real com banco/DICT. As rotas de
exemplo não fazem parte do contrato Pix. O health check mede liveness da API;
não confirma disponibilidade do banco.

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

A API e o migrador usam o código copiado nas imagens, sem bind mount de `src/`.
Depois de alterar código ou migrations, executar novamente `up -d --build --wait`.
Um simples `restart` mantém a versão já construída; não carrega código novo do host.

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

Execução local da etapa 4: **354 testes passaram** (83 anteriores HTTP/PostgreSQL,
229 puros anteriores e 42 casos novos de storage/codec), incluindo os 35 testes HTTP originais. A comparação
separada reproduziu **59 observações** e **2.000 contextos** com decisão completa
igual à referência V3. O CI faz essa comparação montando as fontes somente
para leitura no container de testes. A etapa 4 testa concorrência dos componentes internos, não do fluxo HTTP Pix
completo nem eficácia da política. Ver [evidências da etapa 4](docs/validacao-pr4.md).

Para um ambiente Python local opcional:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -c requirements.lock
```

O venv local exige Python 3.11, a mesma série do Docker/CI. Com Python 3.14,
`psycopg2-binary` 2.9.10 não tem wheel e a instalação a partir do lock falha;
além disso, o teste arquivado da referência para `24:00:00` falha porque o
`fromisoformat` dessa versão aceita o horário. O código de produção rejeita
esse horário e o teste portado passa; são efeitos do interpretador, não
regressões. Sem o stack completo, os testes puros que importam a aplicação em
subprocesso também falham por falta de `fastapi`.

A suíte exige `APP_ENV=testing`, PostgreSQL com nome `pix_test_` e alvo HTTP
explícito em `SERVER_LOCALHOST` e `API_PORT`. Antes de coletar testes, insere
uma entidade sentinela aleatória **somente no banco de testes**, consulta sua
chave pelo GET da API e remove a sentinela em `finally`. Se a API não enxergar
a mesma chave e marcador, a suíte aborta com exit code 4 antes de executar
qualquer teste ou escrita HTTP. Não há fallback para a porta do ambiente de
uso quando `APP_ENV=testing`. Não apontar a suíte para o banco de uso. O caminho
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

## Motor de decisão puro

O enunciado define três status: `approved`, `rejected` e `two_factor_required`,
com topo `key`, `status`, `metadata`. O motor implementa a política
**experimental** `pix-experimental-v3`. Score, cortes, janelas e TTL não são
probabilidades calibradas ou prova de conformidade regulatória.

O motor recebe `Context` e `Policy` imutáveis, com tempo explícito e histórico
já selecionado. Não consulta banco, rede ou relógio. Execução bancária, 2FA e
análise são fatos separados; aprovação e sucesso de 2FA não contam como execução.
O DTO congela os resultados em JSON independente de eventos ou políticas futuros;
a garantia de persistência/replay será integrada nas próximas etapas.

A API carrega `src/antifraud/policies.json` no startup. O catálogo contém apenas
o tenant sintético `demo`; tenant desconhecido falha sem herdar configuração.
Isso ainda não liga tenant à credencial HTTP. Para mudar a configuração, editar
o arquivo e reconstruir a imagem. O `.env.example` deixa `PIX_POLICY_FILE`
comentado: o padrão relativo ao pacote funciona no host e no container. Se
definir um caminho alternativo, ele deve existir no ambiente onde o processo
roda; no Compose, dentro da imagem. Sem recarga dinâmica. Configuração ausente
ou inválida impede o startup.
Pesos não negativos são parametrizados; os pisos de restrição e revogação
permanecem no código. Novo critério exige código e testes.

Com o venv de desenvolvimento instalado, os testes puros não exigem banco:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

[Correções da revisão da PR 2](docs/CORRECOES-REVISAO-PR2.md): achados,
regressões e decisões. O DTO agora recebe a `Decision` pronta, e a auditoria
privada retém o IP inválido para reproduzir o hash.

[Documentação do motor](docs/motor-pix.md): interfaces, configuração,
auditoria, limites e mapa dos critérios. O `Simulator` existe somente nas
fontes e no harness dos testes; não é repository da aplicação.

## Persistência interna Pix — etapa 4

A migration `0002_pix_storage.sql` acrescenta claims, checagem inicial, Pix,
análise imutável, receipts, eventos, vínculos e cache versionado. O serviço
`controllers.pix_storage.PixStorage` coordena as transações; repositories não
dão commit. Reservas de operação e origem têm leases de 10 s e fencing próprios;
locks advisory de origem/destino são ordenados pelos IDs numéricos efetivos.
GET/replay internos devolvem exatamente o DTO persistido; eventos não reescrevem
análise, autenticação não registra device e execução não muda decisão antifraude.

[Contrato, protocolo e limites](docs/storage-pix.md),
[testes e TDD](docs/validacao-pr4.md),
[prompt independente](docs/REVISAO-PR4.md).
“Etapa 4” corresponde ao plano, não à PR #4 de docs já mergeada.
Sem rotas Pix nesta etapa; o serviço interno exige o chamador autorizado.

## Providers e enriquecimento

`Enricher.enrich` é uma biblioteca assíncrona que recebe Context, Policy, deadline
e estado comprovado da checagem inicial (`OWNED` ou `COMPLETED`). Sempre busca
segurança bancária atual e pagamentos confirmados; consulta DICT do destinatário
quando `C_pre < 80`, aproveitando evidência vigente inclusive com C_pre alto.
O resultado conserva cobertura e proveniência na auditoria privada.

A etapa 4 fornece claims com leases/fencing e persistência do resultado
inicial/cache em PostgreSQL. A integração HTTP/enriquecimento sob esse
protocolo e o deadline completo pertencem à etapa 5. Os adapters atuais
são fixtures explícitas, determinísticas e sem rede; não simulam integração
com o DICT real. Não há cache global ou registro de dispositivo por observação.

O orçamento é monotônico: 250 ms por tentativa, no máximo um retry técnico e
3 s totais fornecidos pelo chamador. Adapters async devem cooperar com
cancelamento e não bloquear o event loop. O connector síncrono da base agora
aceita segundos fracionários positivos, sem arredondar `.25` para zero; timeout
de socket não é deadline total. [Contrato e exemplo executável](docs/providers-pix.md).

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
