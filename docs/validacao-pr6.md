# Validação da etapa 6 — aceitação e entrega

Base: `origin/main` em `21a3938defe320ebd7f0f30a51911ca0b1798371`, merge da
PR #6 em 18/10/2026. `main` local atualizada por `pull --ff-only` antes de criar
`test/pix-delivery`. `origin` conferido:
`git@github.com:caiovilquer/qi-tech-hackathon.git`, repositório já público.
Sem deploy, merge automático ou inicialização de Git na pasta pai.

## Escopo efetivamente entregue

- Runner de demonstração que inicia/reinicia Uvicorn em processos reais,
  configura os adapters existentes por arquivo de fixtures e usa POST/GET.
- Banco PostgreSQL próprio por execução, migrations reais e limpeza em sucesso
  e falha de cenário. Nenhum reset/migration sobre o banco fornecido ao runner.
- Sete cenários do guia §11.3, com assertions de status, invariantes e contagens;
  resultado JSON sintetiza observações reais, sem respostas fixas de controller.
- Testes de aceitação do runner, recusa de ambiente de uso e limpeza em falha.
- Serviço Compose com profile `demo`, usando a imagem de testes já existente;
  CI também executa o comando documentado. Sem novo serviço de infraestrutura.
- README no modelo oficial, nomes fornecidos pelo usuário, 25 regras com lógica,
  parâmetros e racional; distinção entre PDF oficial, guia e experimento.
- Matriz do guia §11.2 com arquivos/testes e limites por camada em
  [aceitacao-pix.md](aceitacao-pix.md).

Sem migration nova, alteração de contrato HTTP, pesos, cortes ou dependências.
As fontes em `docs/contexto/` e migrations aplicadas permanecem intactas.

## TDD e revisão própria

1. Antes do runner, os três casos de `test_pix_delivery.py` falharam por
   `ModuleNotFoundError: scripts` (**3 failed em 0,05s**). Comportamentos exigidos:
   demo completa por processo real e recusa de ambiente/DB inadequados antes de
   conectar. Não houve implementação do runner antes desses testes.
2. Implementação mínima usando contratos e serviços existentes: **3 passed em
   2,72s**, HTTP/PostgreSQL real, sem monkeypatch na aplicação.
3. Acrescentado teste de limpeza após falha injetada no cenário. Começou verde:
   cobriu o `finally` já implementado, sem inventar regressão.
4. Execução do comando Compose revelou `KeyError: PIX_POLICY_FILE` quando a
   variável não existia no profile de demo, após refatoração do ambiente filho.
   Corrigido `pop(..., None)`; o teste integrado remove explicitamente a variável
   para manter a regressão coberta. Foco final: **4 passed em 2,73s**.
5. Revisão própria: isolamento de banco/processo/temporários; socket herdado
   elimina corrida de porta; cenário de 2FA usa serviço interno existente e
   não cria endpoint; credenciais e fixtures são sintéticas; docs não atribuem
   calibração ou integração real aos testes. `git diff --check` sem erros.

## Ambiente e situação inicial

Execução local em 18/10/2026, macOS ARM64, Docker **29.8.2**, Compose **5.6.0**;
containers Python **3.11.17** e PostgreSQL **16.15**. Stack preexistente `pix-pr1`
preservada. Projetos novos separados, sem portas de banco para a suíte/demo.

Antes de mudar código: **441 passed em 38,62s**, exit 0, sem skips/xfails, um
warning do `declarative_base` legado. Esse resultado inclui a re-revisão da etapa
5 já mergeada; não é o resultado antigo de 436 casos anterior à re-revisão.

```bash
docker compose -p pix-pr6 -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
# Após a execução, para testar apenas a aceitação:
docker compose -p pix-pr6 -f compose.test.yml run --rm --no-deps tests \
  python -m pytest -q tests/integration/test_pix_delivery.py
```

O desenvolvimento focado montou `scripts/` e `tests/` somente para leitura.
A execução integral e o teste da cópia limpa usam arquivos copiados na imagem,
sem esses mounts. Não confundir o ciclo de desenvolvimento com a evidência final.

## Suíte integral e referência após implementação

Build completa sem mounts de código: **445 passed em 40,38s**, exit 0,
sem skips/xfails e com o mesmo único warning legado. São os 441 casos mergeados
mais 4 da aceitação. A referência original passou separadamente: **92 testes
em 0,129s**; comparação diferencial reproduziu **59 observações** e **2.000
contextos** com `Decision` V3 completa equivalente (não só status).

```bash
docker compose -p pix-pr6 -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr6 -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python -m unittest discover -s /reference -p 'test_*.py' -q
```

### Índices observados no PostgreSQL

Além dos testes de existência dos índices e consulta por janelas, foi executada
uma demo isolada com leitura de `pg_stat_user_indexes` após encerrar os processos
HTTP. Nesse workload sintético: `ix_analysis_decision` **291 scans**,
`ix_pix_destination_time` **21 scans**, `pix_transaction_tenant_id_id_key`
**109 scans**. Outros índices tiveram zero nessa distribuição pequena; o
otimizador pode preferir seq scan em tabelas pequenas. Não é benchmark nem prova
de plano universal. Reprodução independente:

```bash
docker compose -p pix-pr6 -f compose.test.yml run --rm --no-deps tests python - <<'PYCODE'
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import text
from scripts.demo_pix import disposable_database, run, require
with disposable_database() as engine, TemporaryDirectory() as folder:
    run(engine, Path(folder))
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT indexrelname, idx_scan FROM pg_stat_user_indexes WHERE relname IN ('pix_transaction','risk_analysis') ORDER BY indexrelname")).all()
        for name, scans in rows:
            print(name, scans)
        require(any(scans > 0 for name, scans in rows), 'No real index scan observed')
PYCODE
```

## Resultado da demonstração

```bash
docker compose -p pix-demo-final -f compose.test.yml run --build --rm demo
docker compose -p pix-demo-final -f compose.test.yml down -v --remove-orphans
```

Exit 0; saída observada:

| Observação | Resultado |
|---|---|
| Primeiro Pix | approved, remetente not_covered |
| Troca de aparelho | registrado approved; desconhecido two_factor_required |
| Rajada | approved ×4; two_factor_required ×5; rejected ×1 |
| Noite acumulada | approved; two_factor_required; two_factor_required; rejected |
| Adverso/restrito | rejected; PAYEE_DICT_ADVERSE e PAYEE_DICT_RESTRICTED |
| Replay após encerrar/reabrir processo | 200, JSON idêntico com fixtures vazias |
| Após autenticação | 0 pagamentos; 1 evento auth idempotente |
| Após execução | 1 pagamento, 0 maduros; 1 evento execution |
| Aparelho após eventos | unregistered; observação/2FA não autorizam cadastro |
| Análise original após eventos | inalterada |
| Persistência | 21 Pix, 21 análises, 6 checagens iniciais |

As checagens contam contas distintas, mesmo com o mesmo documento de remetente.
Cache de documento não elimina a reserva inicial por conta. A demo não depende
de data fixa de conhecimento: usa hora PostgreSQL e transações sintéticas de
dois dias antes. Datas de abertura de conta são estáveis em 01/01/2020.

## Clone limpo e comandos do README

Um clone **via origin SSH**, sem `.env`, venv, cache ou arquivos de trabalho do
checkout original, foi validado no commit de implementação
`3b6a456b32568c06797e2a6efe2d79467977a52c`:

```bash
git clone --single-branch --branch test/pix-delivery git@github.com:caiovilquer/qi-tech-hackathon.git pr6-clean
cd pr6-clean
git rev-parse HEAD
docker compose -p pix-pr6-clean -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr6-clean-demo -f compose.test.yml run --build --rm demo
API_PORT=3306 DB_PORT=55436 docker compose -p pix-pr6-readme up -d --build --wait
```

- Build/migrations e suíte completa: **445 passed em 40,19s**, exit 0, sem
  skips/xfails e com o único warning legado. Sem bind mounts de código.
- Demo em projeto novo, somente DB + runner: exit 0, mesmas 21 análises e
  invariantes da tabela acima. Não depende da execução prévia da suíte/API.
- Compose de uso com portas locais alternativas: health **204**, payload
  copiado do comando do README com POST **201**, repetição **200** e GET **200**,
  todos com mesmo DTO. Depois de `restart api`, GET **200** idêntico ao original.
- Clone permaneceu limpo. Commits posteriores a esse SHA alteram somente
  documentação de evidências; o código executável é o mesmo.

Os projetos de validação são descartáveis e próprios desta etapa. Removidos
com `down -v --remove-orphans`; o volume/containers preexistentes `pix-pr1`
foram preservados. No projeto de uso real, `down -v` não deve ser executado.

## Como reproduzir e interpretar

Os comandos integrais e de referência estão no README. Referência Python é
executada separadamente com os originais montados somente para leitura. Os
logs locais ficam fora do Git, em `../tmp/`, sem dependência deles para revisão.
A futura execução em outro host pode produzir tempos/versões de patch diferentes.
O CI reproduz a stack Linux; só registrar sucesso depois de confirmar seu run.

Esta etapa fecha a aceitação técnica e a documentação planejadas. A PR ainda
requer revisão independente e correções pertinentes antes de aprovação. A
apresentação oral, conexão a bancos/DICT reais e deploy não foram executados.
Não há garantia de eficácia antifraude ou conformidade regulatória demonstrada.
