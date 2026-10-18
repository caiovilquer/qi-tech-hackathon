# Migrations PostgreSQL

O runner usa SQLAlchemy/psycopg2 já presentes na base. Não acrescenta framework,
serviço ou dependência. Ele executa somente migrations SQL confiáveis do
checkout; não é um mecanismo de SQL enviado pelo cliente.

## Protocolo

1. Exigir PostgreSQL e arquivos consecutivos `0001_nome.sql`, `0002_nome.sql`.
2. Abrir transação, fixar search_path em public e adquirir advisory lock
   transacional exclusivo do migrador (ID `73412690586274901`).
3. Verificar `schema_migration`: nomes e SHA-256 devem ser prefixo exato dos
   arquivos locais. Recusar arquivo aplicado alterado, removido ou desconhecido.
4. Executar SQL pendente e registrar checksum/instante no mesmo commit.
5. Confirmar tudo junto; qualquer erro reverte DDL, dados e histórico do lote.

O lock espera até 10 s; cada statement tem timeout de 30 s. Timeouts fazem a
operação falhar sem sucesso parcial. O operador deve verificar a causa e
repetir. Não há polling infinito ou reparo silencioso. Os locks de migração
não são o protocolo de concorrência Pix, que pertence a outra etapa.

Migrations não podem conter COMMIT/ROLLBACK, operações não transacionais
(como CREATE INDEX CONCURRENTLY) ou efeitos externos. São scripts de schema
versionados e revisados. Não modificar manualmente `schema_migration`. Não há
downgrade; mudanças destrutivas futuras exigem estratégia própria de dados.

## Banco novo

```bash
docker compose up -d --build --wait
```

PostgreSQL inicia sem initializer do schema. O serviço `migrate` cria as três
tabelas da base e os quatro status; a API depende de sua conclusão com sucesso.
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
colunas/ordem/tipos/nulabilidade/defaults, PKs, uniques, FKs e CHECKs, além dos
quatro enumeradores. Nomes de constraints podem diferir. Dados da entidade,
IDs e sequências existentes permanecem; não há DROP/recriação das tabelas de
uso. Estrutura parcial ou diferente causa erro, sem marcar versão aplicada.
O schema temporário de referência é removido no sucesso ou rollback da falha.
Adoção exige permissão de criar schema; upgrades normais exigem as permissões
de DDL previstas no SQL.

Essa comparação valida a estrutura inicial, não é auditoria de triggers,
permissões ou conteúdo arbitrário de um banco desconhecido. Em caso de
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
