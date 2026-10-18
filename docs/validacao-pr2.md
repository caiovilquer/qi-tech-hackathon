# Evidências e rastreabilidade — PR 2

Base: `main` atualizada por `git pull --ff-only origin main`, após confirmar
PR 1 mergeada em `c35005af40590fe9b60023ab29ac62d85bb9ff0f` e checkout limpo.
Branch `feat/pix-engine` criada a partir desse commit. `origin` conferido:
`git@github.com:caiovilquer/qi-tech-hackathon.git`, repositório público já
existente. Nenhum Git ou temporário adicionado na pasta pai.

Validação em 18/10/2026, macOS ARM64, Python 3.11.15 e Docker Desktop;
PostgreSQL 16.15 real na stack descartável. Dados exclusivamente sintéticos.
O SHA exato submetido está na descrição da PR e no resultado da entrega;
confira também o SHA dos checks, não apenas o nome da branch.

## Baseline aprovado

Antes das mudanças desta etapa:

```bash
docker compose -p pix-pr2-baseline -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
```

**83 passaram em 3,66 s**, sem skips/xfails, PostgreSQL real.
A base inclui as correções da revisão independente anterior. Migrations,
guardas destrutivas e os 35 testes HTTP originais não foram modificados.

## Suíte da implementação

```bash
docker compose -p pix-pr2-test -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
# Depois da comparação documental abaixo:
docker compose -p pix-pr2-test -f compose.test.yml down -v --remove-orphans
```

**186 passaram em 4,49 s**, sem skips/xfails (83 anteriores + 103 testes puros).
Inclui startup válido/inválido em subprocesso e o HTTP/PostgreSQL do exemplo
original, sem enfraquecer a guarda de ambiente. A duração e o check Linux do
commit submetido devem ser conferidos na PR. O pytest completo continua
exigindo banco/API dedicados; não usar o banco do ambiente de uso.

A suíte pura também roda sem PostgreSQL ou API, com dependências de dev:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
```

**103 passaram**. Dois testes importam a aplicação em subprocesso com URL de
banco deliberadamente inacessível: importar/carregar configuração não consulta
o banco. Um caso inválido aborta com `PolicyConfigurationError` antes de servir;
o válido verifica catálogo e preservação das rotas existentes, sem rota Pix.

## Rastreabilidade dos 92 testes recebidos

| Fonte | Portado para implementação | Evidência | Limite |
|---|---|---|---|
| `test_contrato_pix`: 27 ContractTests + 9 StrictJsonTests | `tests/unit/test_pix_contract.py`: 36 | Mesmos métodos/asserts usando `antifraud.contract` e schema de runtime | Parsing da biblioteca; ainda sem HTTP Pix |
| `test_politica_pix.PolicyTests`: 36 | `tests/unit/test_pix_policy_reference.py`: 36 | Mesmos invariantes usando tipos/motor de produção e montagem de contexto em `tests/support` | Histórico e eventos fornecidos em memória |
| `test_politica_pix.OrchestrationTests`: 20 | Não portados como comportamento de produção | Continuam executáveis somente na referência original | DICT inicial/cache/versões: PR 3; eventos/storage: PR 4; idempotência/reserva/HTTP: PRs 4/5 |
| Novos testes da PR 2 | 31 | `test_pix_engine.py` e `test_pix_configuration.py` | Biblioteca e configuração/startup; não integração bancária |

Nomes e asserts dos 72 métodos portados são preservados; só imports,
docstrings e a delimitação das classes mudaram. Os 20 testes sequenciais de
orquestração não são substitutos dos testes concorrentes/PostgreSQL futuros.
`tests/support/pix_scenarios.py` reaproveita somente montagem de fixtures,
cenários e simulador sequencial para alimentar o motor **de produção**.
Nenhum módulo em `src/` importa o Simulator ou `docs/contexto`.

Regressões novas: configuração desconhecida/incompleta/tipos/limiares/janelas,
tenants/versões isolados, cópias/fingerprint estáveis, JSON duplicado/não finito,
startup fail-fast, DTO sem dados privados, snapshot congelado, inputs imutáveis,
consulta necessária sem evidência, indisponibilidade uma única vez, coeficiente
preliminar com pesos diferentes, pisos com score zero, medianas fracionárias,
UTC no extremo do contrato e 24h decorridas atravessando horário de verão.

## Comparação diferencial e fonte original

No host com venv:

```bash
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

- **92 testes originais passaram**, sem alterar as fontes.
- **59 observações** do motor portado iguais ao JSON arquivado, campo a campo.
- **2.000 contextos**, semente `20261018`, comparados com `evaluate` V3 original:
  `Decision` completa igual, incluindo ordem de motivos, pontos/pisos, famílias,
  score, C_pre e features públicas. Varia PF/identidades, tenants de histórico,
  quantias, horários, janelas, estados de segurança/DICT, eventos e maturidade.
  O gerador diferencial não cobre todas as combinações nem prova ausência de bugs.

No container, **reconstruir antes** e montar as fontes somente para leitura:

```bash
docker compose -p pix-pr2-test -f compose.test.yml run --rm -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
```

A imagem exclui `docs/contexto`, que é somente oráculo documental desse comando.
Esse passo também está no CI, após o build e a suíte HTTP/PostgreSQL. Rodar
contra imagem antiga não vê scripts novos; sempre fazer o `up --build` anterior.
O script não chama o entrypoint que sobrescreve o JSON original. Os desvios
defensivos de DICT ausente/aritmética UTC estão documentados em `motor-pix.md`
e têm asserts próprios, sem forçar igualdade com bugs da referência.

## Revisão própria e limites

- Diff de base, startup, catálogo, parser, regras, DTO e testes revisado;
  `git diff --check`, compilação, schema igual à fonte e hashes documentais.
- Módulos puros não importam SQLAlchemy, HTTP clients ou FastAPI; o teste
  bloqueia abertura de arquivos e conexão de rede durante avaliações repetidas,
  verificando decisão determinística e ausência de mutação.
- Corrigidos na revisão própria: configuração mutável vazia escapando da
  validação direta, tipos aninhados mutáveis, precisão do snapshot de mediana,
  projeção pública de dados sensíveis e aritmética em timezone local.
- Nenhuma migration, endpoint Pix, adapter de provider, ingestão bancária ou
  persistência/lock/claim Pix entregue. O banco real valida regressões da base;
  não declarar garantias Pix de concorrência ou recuperação com esses resultados.
- Política experimental, sem integração DICT real, eficácia/calibração ou
  conformidade comprovada. Aprovação/2FA nunca foi apresentada como execução.

A próxima etapa permanece bloqueada pela revisão e aprovação desta PR.
Os comentários da revisão serão respondidos individualmente no GitHub com
correção/justificativa, commit e evidências, conforme regra do projeto.
