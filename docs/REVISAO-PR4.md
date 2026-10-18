# Prompt de revisão independente — etapa 4 (storage)

Revise a PR da branch `feat/pix-storage` no repositório
`caiovilquer/qi-tech-hackathon`. Link e SHA exatos estão na descrição da PR;
confira com `gh pr view feat/pix-storage --repo caiovilquer/qi-tech-hackathon --json url,headRefOid`.
Registre o SHA efetivamente revisado. A PR #4 existente é apenas documentação;
esta é a etapa 4 do plano e terá outro número GitHub. Base:
`d7cdce82e31abc13c7881cb2dc5268775601828f`, main após PR 3 + PR #4 de docs.

Procure bugs, regressões, falhas de segurança, inconsistências e testes ausentes
com evidência. Não tome testes verdes como prova de ausência de bugs. Não
altere código, faça merge ou implemente a próxima etapa.

## Objetivo e limites

Entregar migrations e APIs internas reais de persistência/coordenação Pix:
reserva tenant/ID/hash antes de providers, checagem inicial por conta com lease
própria, fencing independente, histórico/eventos/cache versionado, locks de
origem/destino e commit atômico de análise/DTO/claim/receipt. Nenhum endpoint
Pix, provider remoto ou fluxo HTTP completo é declarado pronto.

Sessões SQLAlchemy seguem a base, repository recebe contexto e não commita;
o serviço interno da camada controller coordena transações curtas e finais.
DDL é explícita; mappings Core são para queries, não create_all/migrations.
Não exigir funcionalidades futuras como se estivessem entregues, nem aceitar
falha de garantia declarada por estar rotulada como futura em outro lugar.

## Critérios de aceitação

1. Migration 0002 cria schema interno sem alterar 0001/Base API/dados existentes.
   Dinheiro BIGINT em centavos; tempos aware/TIMESTAMPTZ; constraints/FKs/índices
   e unique uq_pix_tenant_external_id. Legado validado e banco 0001 evoluem.
2. Claim confirma hash antes de qualquer enriquecimento; conflito em voo é
   distinguível de Busy. Takeover só após lease, fence cresce, dono antigo não
   grava. GET/replay só devolvem análise concluída no tenant e DTO original.
3. Checagem inicial tem estado/dono/token/lease por AccountKey; outro dono ativo
   não é completed. Conclusão verifica tanto token da origem quanto operação;
   resultado tipado inclusive unavailable é durável/imutável. Mudança de titular
   não soma perfis silenciosamente; troca de device não reinicia origem.
4. READ COMMITTED, locks advisory de origem/destino em ordem dos int64 reais
   e deduplicados. Mesmo destino protege agregado entre origens diferentes.
   Eventos que alteram exposição/perfil seguem protocolo de locks compatível.
5. Writer relê contexto local sob locks e exige decision_at após aquisição,
   payload/hash/pix correspondentes e checagem inicial concluída. Motor puro
   calcula decisão sem rede/SQL/clock. Pix/análise/DTO/claim/receipt têm um commit;
   flush/commit falhos não retornam sucesso nem deixam parcial. Original imutável.
6. Eventos internos tenant/provider/event_id idempotentes; hash diferente é
   QIT002006; vínculo/transição inválidos QIT002007. Challenge inclui tenant,
   Pix, key e fingerprint financeiro. Falha antiga não revive terminal;
   failed/cancelled não apagam executed; ocorrência terminal não é deslocada.
7. Decisão, resultado de autenticação e execução financeira separados. 2FA não
   registra device; GET original não vira approved por evento. Falha terminal
   só libera exposição do desafio exato. Fato executado não é inventado por
   aprovação nem descartado apenas porque análise era rejeitada.
8. Cache por tenant/sujeito/operação/permissão/provider/versão; snapshot por
   observed_at/source_version e conhecimento por available_at/received_at local.
   Watermark vencido não dá rollback para limpeza antiga; mesma revisão
   conflitante falha. Falha/ausência inicial durável não é cache limpo.
9. História SQL escopada por tenant/conta/destino e janelas, com conhecimento
   temporal. Codec não executa conteúdo. Canônico com IP_INVALID/null continua
   legível, bruto fica só na auditoria. Receipts mínimos sem token/headers,
   identidade original preservada e referência a análise só do mesmo tenant.
10. Base HTTP e testes anteriores preservados. Sem Redis, fila, novos serviços
    ou dependências. Recursos HTTP atuais não são promovidos ao contrato Pix.
    Deadline completo, loops/revalidação até três ciclos e crash HTTP ficam
    na etapa 5, com integração de progresso da etapa 3 e posse da etapa 4.

## Arquivos e contexto

Leia PLANO-IMPLEMENTACAO.md, README.md, docs/storage-pix.md,
docs/validacao-pr4.md e docs/migrations.md. Fontes em docs/contexto:
guia V3 §§3–5, 7 e 8, enunciado oficial PDF pp19–25 e revisão independente do
guia. Oficial, decisões do guia e parâmetros experimentais são distintos.
Histórico das interfaces de providers: docs/providers-pix.md e correções PR 3.

Diff principal: migrations/0002_pix_storage.sql; src/models/pix.py,
src/repositories/pix_repository.py, src/controllers/pix_storage.py,
src/dtos/pix_storage.py, src/utils/pix_storage_codec.py;
tests/integration/test_pix_storage.py e adaptação dos ensaios do runner em
tests/integration/test_migrations.py. Rastreie chamadas até banco, não só helpers.

Conferir especialmente: corrida de reserva com hashes diferentes, token de
operação perdido enquanto token da origem ainda vive, estado completed/lease,
validação de contexto pós-lock, commit que falha de verdade, race de evento ID
entre operações, terminal auth entregue fora de ordem, filtros de conhecimento,
proveniência/cache conflitante, tipos monetários e vazamentos cross-tenant.

## Reprodução

Na raiz de clone com Docker/Compose v2 e daemon iniciado:

```bash
git fetch origin
git switch --detach origin/feat/pix-storage
git rev-parse HEAD
docker compose -p pix-storage-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-storage-review -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/integration/test_pix_storage.py
docker compose -p pix-storage-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-storage-review -f compose.test.yml down -v --remove-orphans
```

Esperado: 354 totais (312 anteriores +42 storage/codec), 229 puros anteriores,
92 originais e diferencial 59/2.000. Há um warning da base declarative_base.
Testes novos criam/removem bancos isolados; não apontar para banco de uso.
Não usar Python 3.14 para inferir regressão de dependências fixadas em 3.11.

Para cada achado: prioridade, arquivo/linha, requisito violado, gatilho,
reprodução/evidência, impacto e sugestão. Distinga bug da etapa, integração
futura e melhoria opcional. Se não houver achados, diga isso e registre limites.
As correções serão feitas na mesma PR, com testes e respostas individuais;
não resolver threads ou avançar etapas sem a aprovação do usuário.
