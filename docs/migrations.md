# Migrations PostgreSQL

O runner usa SQLAlchemy/psycopg2 já presentes na base. Não acrescenta framework,
serviço ou dependência. Ele executa somente migrations SQL confiáveis do
checkout; não é um mecanismo de SQL enviado pelo cliente.

## Protocolo

1. Exigir PostgreSQL e arquivos consecutivos `0001_nome.sql`, `0002_nome.sql`.
2. Abrir transação em `READ COMMITTED` explícito, fixar search_path em public e adquirir advisory lock
   transacional exclusivo do migrador (ID `73412690586274901`).
3. Verificar `schema_migration`: nomes e SHA-256 devem ser prefixo exato dos
   arquivos locais. Recusar arquivo aplicado alterado, removido ou desconhecido.
4. Executar SQL pendente sem interpolação DBAPI de `%` e registrar
   checksum/instante no mesmo commit. Literais `%`, `%%`, LIKE e PL/pgSQL
   chegam ao PostgreSQL sem substituição de placeholders.
5. Confirmar tudo junto; para o SQL transacional permitido, qualquer erro
   reverte DDL, dados e histórico do lote.

O lock espera até 10 s; cada statement tem timeout de 30 s. Timeouts fazem a
operação falhar sem sucesso parcial. O operador deve verificar a causa e
repetir. Não há polling infinito ou reparo silencioso. Os locks de migração
são distintos do protocolo de concorrência Pix de [storage-pix.md](storage-pix.md).

Migrations não podem conter COMMIT/ROLLBACK, operações não transacionais
(como CREATE INDEX CONCURRENTLY) ou efeitos externos. São scripts de schema
versionados e revisados. Não modificar manualmente `schema_migration`. Não há
downgrade; mudanças destrutivas futuras exigem estratégia própria de dados.
O runner não é um parser ou sandbox de SQL e não detecta um COMMIT escrito
no arquivo. Um script que viole esse contrato pode causar persistência
parcial. Conferir o advisory lock depois de executar não impede um COMMIT
anterior: o dado já foi confirmado. A revisão do SQL antes da execução é
necessária; não se promete atomicidade para scripts com controle transacional
próprio ou efeitos externos.

## Banco novo

```bash
docker compose up -d --build --wait
```

PostgreSQL inicia sem initializer do schema. O serviço `migrate` cria as três
tabelas da base e os quatro status (0001), seguidos das dez tabelas internas
Pix, índices, constraints e guards de imutabilidade (0002). A API depende da
conclusão com sucesso; isso não habilita rotas Pix.
O volume nomeado passa a preservar os dados. A API em si não executa DDL.

## Banco já inicializado pela Base API

Não executar `stamp` ou marcar versão sem conferir o schema. Com a API parada
durante a adoção e backup adequado ao ambiente, apontar `DATABASE_URL` para o
banco correto e executar na raiz do checkout:

```bash
.venv/bin/python -m migrations --adopt-base
```

O Compose chama a mesma opção. Se não houver tabelas da base, ela cria o schema
normalmente. Se houver, a adoção confere as três tabelas contra a DDL inicial
real, executada em um schema de referência isolado na mesma transação:
colunas/ordem/tipos/nulabilidade/defaults/collations, PKs, unique constraints,
índices únicos (chaves, expressões, predicado, opclasses, collations, opções e
estado), FKs/CHECKs e sua validação/deferrabilidade, além dos quatro enumeradores.
Os nomes de constraints e índices podem diferir. Índices não únicos adicionais
são permitidos: não mudam a unicidade das escritas. Adoção recusa triggers de
usuário e RLS, que não existem na base original. As opções das sequências devem
corresponder à DDL e o próximo ID precisa ser maior que o máximo existente;
sequência atrasada é recusada, sem reajuste silencioso. Dados da entidade,
IDs e sequências existentes permanecem; não há DROP/recriação das tabelas de
uso. Estrutura parcial ou diferente causa erro, sem marcar versão aplicada.
O schema temporário de referência é removido no sucesso ou rollback da falha.
Adoção exige permissão de criar schema; upgrades normais exigem as permissões
de DDL previstas no SQL.

Essa comparação valida a estrutura inicial e recusa triggers de usuário/RLS;
não é auditoria de permissões ou conteúdo arbitrário de um banco desconhecido. Em caso de
incompatibilidade, inspecionar e preparar uma migração explícita; não apagar
dados ou alterar checksum para forçar o startup.

O Compose novo cria um volume nomeado. Ele não descobre nem transfere dados de
volumes anônimos de instalações antigas. Para um ambiente existente, preservar
o volume e realizar uma transferência/backup/restore deliberada ou executar o
runner diretamente contra o banco existente. Não remover volumes para tentar
corrigir um erro de migration.

## Evolução e testes

Adicionar a próxima migration SQL com número consecutivo e alterações
compatíveis. Rodar a stack isolada do README. A suíte verifica banco vazio,
upgrade com dados existentes, adoção, drift, execução repetida/concorrente,
checksum, histórico desconhecido e rollback do lote. Não usa SQLite.
`database/database.sql` continua como arquivo original da base, mas os resets
de testes e a inicialização atual aplicam as migrations.

## 0002 — persistência interna Pix

Acrescenta pix_account, analysis_claim, source_initial_check, pix_transaction,
risk_analysis, request_receipt, bank_challenge, bank_operation_link, bank_event
e provider_snapshot. Não modifica tabelas/dados da base. Um banco versionado
em 0001 recebe só 0002; banco legado validado recebe adoção de 0001 e criação
de 0002 na mesma transação. Constraints/FKs carregam tenant; a unique de
idempotência se chama uq_pix_tenant_external_id.

Valores financeiros usam BIGINT em centavos; tempos usam TIMESTAMPTZ e
conhecimento de ingestão fica separado da ocorrência. Triggers proíbem
UPDATE/DELETE dos fatos originais, de análises e de reservas concluídas;
o hash reservado de uma operação nunca é alterado. Cache é material derivado:
metadados de mesmo fato podem conservar primeiro conhecimento/staleness,
sem renovar validade nem apagar versões antigas.

A migration não deve ser reaplicada/editada depois de publicada. Os testes
adversariais do runner fixam sua DDL-base 0001 para poder adicionar uma 0002
sintética; a suíte nova verifica a cadeia real completa, upgrade de 0001,
adoção de legado, tipos/modelos e funcionamento dos componentes Pix.
Não aplicar ao ambiente de uso para “testar”; compose.test.yml cria banco
isolado e os testes novos usam bancos aleatórios adicionais nesse servidor.
