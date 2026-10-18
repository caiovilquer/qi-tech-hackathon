# Correções da revisão da PR 1

Revisão recebida no snapshot `9fa01b3748156945a37382dc4d1dccab13bd39b3`:
[relatório do revisor](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#pullrequestreview-5561332534).
Foram lidos o relatório completo, os 12 comentários inline e os comentários
gerais da PR (nenhum comentário geral adicional no momento da conferência).
As correções ficam na mesma branch `feat/pix-foundation`; nenhuma etapa
seguinte foi iniciada. O SHA exato da rodada atual fica na descrição da PR.

## Achados obrigatórios

| ID | Status | Correção e evidência |
|---|---|---|
| F1, P1 | Corrigido | `no_parameters=True` nos dois caminhos que executam SQL de arquivo: upgrade e referência da adoção. Testes com `100%`, `100%%`, `format('%s', ...)`, LIKE, trigger PL/pgSQL com RAISE e comentário `%` no DDL inicial. O conteúdo executado e persistido permanece literal. |
| F2, P2 | Corrigido | Alvo HTTP explícito em testing, sem fallback. Antes da coleta, sentinela aleatória gravada só no PostgreSQL dedicado, lida por GET autenticado e removida em finally. Teste subprocesso com API lendo outro banco retorna exit 4, sem executar o teste HTTP; contagem do banco da API não muda e sentinela não sobra no banco de teste. Falha de autenticação também limpa a sentinela. |
| F3, P2 | Corrigido | Removido bind mount de src do Compose de uso. API e migrador usam imagens construídas do mesmo checkout. Reprodução em cópia descartável: alterar código/0002 no host sem rebuild não altera nenhum deles; rebuild migra e inicia a API correspondente. README exige `--build` após mudanças. Não foi adicionado ambiente extra de hot reload. |
| F4, P2 | Corrigido | Catálogos comparam collations, índices únicos (inclusive expressão/parcial), validade/deferrabilidade das constraints e opções das sequências. Também recusam trigger de usuário, RLS e sequência atrasada em relação a max(id), sem reparar dados automaticamente. Todos esses drifts têm testes de recusa e preservação da linha original. |
| F5, P3 | Corrigido | Transação do migrador fixa READ COMMITTED antes do lock. Concorrência em banco vazio e upgrade só de dados, com defaults do servidor read committed/repeatable read/serializable: uma execução aplica e a outra retorna vazia; SQL verifica que o isolamento efetivo é read committed. |
| F6, P3 | Corrigido | A suíte acrescenta os casos de %, adoção em banco vazio com flag, novos drifts e concorrência sob os três defaults. O migrador do Compose de testes usa `--adopt-base`, como o de uso. |
| F7, P3 | Corrigido | Prompt deixa de pressupor caminho macOS do autor; aponta para raiz de um clone. docs/migrations.md descreve precisamente os novos catálogos comparados, os índices não únicos permitidos e os limites reais. |

Rastreabilidade dos comentários:

- F1: [execução](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629889)
  e [adoção](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629903).
- F2: [guarda](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629906)
  e [fallback HTTP](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629909).
- F3: [bind mount](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629912).
- F4: [assinatura](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629914).
- F5: [isolamento](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629915).
- F6: [lacunas](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629920).
- F7: [caminho do prompt](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629932)
  e [documentação da adoção](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629940).

## Sugestões opcionais

| Sugestão | Decisão e justificativa |
|---|---|
| [Verificar lock depois do SQL para detectar COMMIT](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629925) | Não adotada. A proposta não evita persistência parcial: depois de um COMMIT o DDL anterior já foi confirmado. Reprodução própria em banco descartável confirmou `half` persistida, `other` ausente e histórico só com 0001 após divisão por zero. A checagem tardia não pode desfazer isso. O contrato continua restrito a scripts confiáveis e estritamente transacionais, sem COMMIT/ROLLBACK/efeitos externos; docs/migrations.md explicita que o runner não é parser/sandbox e não promete atomicidade fora desse contrato. Uma proteção completa exigiria preflight SQL adequado; não foi adicionada uma regex incompleta que pudesse sugerir essa garantia. |
| [DROP DATABASE WITH FORCE no teardown](https://github.com/caiovilquer/qi-tech-hackathon/pull/1#discussion_r4305629929) | Adotada. Aplica-se somente aos bancos aleatórios criados e possuídos pelo fixture no servidor descartável; não altera cleanup de bancos do ambiente de uso. |
| Fixar search_path da API (relatório geral) | Adotada. `src/database.py` configura `search_path=public` ao conectar. Teste em processo separado cria schema homônimo ao role, com tabela homônima, e comprova que a conexão da API usa public. |

Não há achado obrigatório classificado como improcedente ou pendente nesta
rodada. A sugestão opcional não adotada é mantida acima como limite explícito
para a re-revisão; não foi apresentada como implementada.

## Testes e reprodução

Antes das correções, os novos casos de migration produziram **18 falhas e
59 sucessos**: %, drift e isolamento reproduziram os defeitos no código anterior.
Após as correções e os casos da guarda/search_path: **83 passaram em 3,70 s**,
sem skips/xfails, em PostgreSQL 16.15 real no Docker/macOS ARM64. Inclui os
35 testes HTTP originais, sem alterar seus arquivos. Esse tempo é do pytest,
não do build. O resultado remoto do novo SHA está nos checks da PR.

Na raiz de um clone com Docker/Compose:

```bash
docker compose -p pix-rereview -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-rereview -f compose.test.yml down -v --remove-orphans
```

A matriz está em `tests/integration/test_migrations.py` e
`tests/integration/test_environment_guard.py`. O teste negativo da guarda usa
somente dois bancos de teste: o da API e outro aleatório. Não foi necessário
escrever em um banco de uso para reproduzir o problema.

Também executados: 92 testes da referência, sem skips; hashes dos 24 arquivos
de contexto conferidos e inalterados; comparação da DDL inicial com a original;
compilação Python e revisão do diff. O smoke de imagem foi executado em uma
cópia temporária fora do Git com portas 3302/55434 e projeto próprio, removido
com seu volume ao terminar. Os logs temporários ficaram fora do repositório.

O snapshot anterior continua preservado no histórico. `src/database.py` é o
único arquivo de runtime da base alterado nesta rodada, apenas para configuração
de conexão; camadas, autenticação e contratos da entidade permanecem. Não há
motor, endpoint ou persistência Pix entregues nesta PR.

## Pedido de re-revisão

Usar o [prompt atualizado](REVISAO-PR1.md), conferir o headRefOid atual da PR e
registrar o SHA efetivamente revisado. Comparar `9fa01b3..HEAD`, reproduzir os
achados originais e procurar falhas/regressões nas correções, inclusive na
limpeza da sentinela e na adoção. Não inferir ausência de bugs dos testes
passando. Aguardar aprovação explícita antes da próxima etapa; nenhum merge
ou deploy foi executado.
