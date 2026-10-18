# Evidências e TDD — etapa 4 (storage)

Base: `origin/main` `d7cdce82e31abc13c7881cb2dc5268775601828f`, incluindo o
merge da PR 3 e da PR #4 adicional de docs. Checkout inicial limpo;
`git switch main`, `git pull --ff-only origin main`, `feat/pix-storage` criada
a partir dessa base. Origin SSH existente conferido antes de publicar.
Nenhum Git na pasta pai, deploy ou merge feito nesta etapa.

“Etapa 4/PR 4” identifica o plano, não a PR #4 de documentação já mergeada.
O número GitHub, branch e SHA efetivo de revisão estão na descrição da nova PR.

## Execução

Baseline em 18/10/2026, macOS ARM64/Docker, Python 3.11 e PostgreSQL 16:
**312 testes passaram em 5,08 s** na stack `pix-pr4-baseline`, sem skips/xfails.
Ambiente de uso `pix-pr1` preservado; stacks novas não publicam portas e usam
PostgreSQL descartável. Cada teste novo de storage cria banco aleatório
adicional no servidor de testes e o remove ao terminar, inclusive após erro.

Após implementação: **354 testes passaram em 7,25 s**, com PostgreSQL
**16.15** real: 312 anteriores e 42 casos novos
em `tests/integration/test_pix_storage.py`. Há um aviso de depreciação ao importar
o declarative_base original da Base API; não é falha/skip. O CI da branch complementa essa evidência local no SHA exato da revisão.

- **229 testes puros anteriores** passam no venv Python 3.11.15.
- **92 testes originais** passam, fontes preservadas.
- **59 observações** e **2.000 contextos** continuam idênticos à Decision V3
  completa. Novas funções de storage têm invariantes próprios; a referência
  sequencial não prova SQL, locks, migrations ou HTTP.
- Código compilado, diff revisado, `git diff --check` e 24 hashes de fontes
  conferidos. Migration 0001 e schema oficial não alterados.

## Sequência TDD e revisão própria

Os 16 testes iniciais de contratos foram escritos antes dos componentes.
Primeiro RED por `ModuleNotFoundError: controllers.pix_storage`, não por
asserts SQL. A primeira implementação passou 15; uma expectativa errada do
ensaio de perfil exigia maturidade de uma execução recém-conhecida. Corrigido
o teste para distinguir pagamento executado de maduro; não alterado o motor.

Na revisão própria, testes adicionais registraram falhas antes dos ajustes:

- Context local anterior aos locks/desatualizado era aceito; agora é recusado.
- Comparação dos mappings/DDL revelou VARCHAR em hash CHAR; tipos alinhados.
- Receipt de replay ainda não aceitava referência à análise: RED de interface,
  seguido da implementação com escopo de tenant e nenhuma nova tentativa.
- Reparse do canônico com IP_INVALID/null como se fosse payload HTTP novo
  quebrava o histórico/eventos. Reproduzido e corrigido para colunas tipadas.
- Mesma execução terminal com ocorrência diferente e receipt de outra operação
  eram aceitos (dois asserts RED). Recusados; tempo original do receipt mantido.
- Leitura com datetime sem timezone era aceita; agora falha antes do SQL.
- Lotes de cache com mesma observação e versões em ordem inversa podiam
  entrar em deadlock. Teste com sessões reais reproduziu a espera circular;
  ordenação inclui source_version, além de tenant/chave/observação.
- Score configurado validamente acima de int32 falhava no INSERT. Teste
  reproduziu overflow e o campo passou a NUMERIC com CHECK de inteiro não
  negativo, sem limitar a política; dinheiro continua BIGINT em centavos.
- Tipo inválido de analysis_key em evento escapava como AttributeError; passou
  a InvalidEvent, com validação de escalares, UUID, NUL e faixa de versão.

Outros casos começaram verdes por verificarem comportamento já correto:
claims concorrentes, leases/fencing separados, conflitos de hash/evento,
rollback após inserts e no commit real, ordem de eventos, terminal de 2FA,
mesmo destino com pagadores distintos e eventos financeiros independentes da
rejeição original. Não se fabricou falha para declarar TDD nem se alegou que
42 casos eram 42 bugs. Não há placeholders no comportamento entregue.

## Cobertura e significado da prova

- **Claims:** dono único, hash em voo/permanente, takeover crescente, dono antigo
  impedido, origem por conta/tenant, token de origem independente e verificação
  do token da operação ao concluir origem. Lease é expirada deliberadamente por
  SQL no banco isolado para testar fencing sem esperar 10 s por cenário.
- **Atomicidade:** exceção depois de todos os inserts, erro SQL e constraint
  trigger realmente deferred que falha **no commit PostgreSQL**. Não sobra Pix,
  análise, claim completed ou receipt parcialmente finalizado.
- **Locks:** timeout SQLSTATE 55P03 em sessões concorrentes; pagadores distintos
  com um único destino compartilham o lock de destino. IDs int64 efetivamente
  travados estão ordenados/deduplicados. Transações usam READ COMMITTED explícito.
- **Eventos:** replay/conflito por tenant/provider/event_id, corrida do mesmo ID
  em operações diferentes, vínculo key/fingerprint/desafio, terminal financeiro
  imutável, ordem de versões auth, late failed não terminal, expired e liberação
  da exposição exata. 2FA/execução nunca reescrevem a análise nem cadastram device.
- **Cache/histórico:** escopo, versão, watermark vencido, conflito concorrente,
  conhecimento local/ocorrência, recibos por decision_at, janela de 90 dias,
  índices e round-trip de payload/DTO/codec. Não truncar agregados para “acelerar”.
- **Migrations:** vazia, Base API legado não vazio, 0001 versionada para 0002,
  idempotência e catálogo/types. Testes adversariais do runner passaram a fixar
  explicitamente 0001 para adicionar 0002 sintética, mantendo as guardas antigas;
  novos testes verificam a cadeia real completa. Nenhuma guarda foi removida.

Essa prova é de componentes internos com SQL real. Não cobre POST/GET Pix,
loop HTTP de wait/revalidação, crash de processo durante fluxo completo,
commit remoto desconhecido resolvido pelo endpoint ou autenticação de um novo
canal de eventos: essas integrações continuam nas etapas 5/6, sem endpoint
artificial para declarar que já funcionam. Não há calibração/conformidade
comprovada nem DICT real. Nenhum bug obrigatório conhecido foi adiado.

## Reprodução

Com Docker/Compose v2 e daemon disponível, na raiz deste Git:

```bash
docker compose -p pix-storage-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-storage-review -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/integration/test_pix_storage.py
docker compose -p pix-storage-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-storage-review -f compose.test.yml down -v --remove-orphans
```

Não usar ambiente de aplicação para executar testes/reset. O guard existente
confere APP_ENV=testing, nome pix_test_ e sentinela HTTP/banco antes da coleta.
Os testes de storage são síncronos e usam sessões concorrentes reais; não
substituir PostgreSQL por SQLite ou mock de repository para validar esta etapa.
