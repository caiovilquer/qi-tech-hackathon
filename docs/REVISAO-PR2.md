# Prompt de revisão independente da PR 2

Revise a segunda etapa da API Antifraude Pix, procurando bugs, regressões,
falhas de segurança, inconsistências e testes ausentes. Não tome testes
passando ou equivalência com o simulador como prova de ausência de bugs.

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/2
Branch: `feat/pix-engine`
Base: `main`, commit `c35005af40590fe9b60023ab29ac62d85bb9ff0f`
SHA: revisar o commit exato informado na descrição da PR; confirmar com
`gh pr view 2 --repo caiovilquer/qi-tech-hackathon --json headRefOid --jq .headRefOid`
e registrar esse SHA no relatório. O prompt fica versionado antes do commit
para não criar referência circular ao próprio hash.

Para a re-revisão, comece por `docs/CORRECOES-REVISAO-PR2.md` e pelo diff
`git diff c6b7eb5..HEAD`. Há nove achados corrigidos, a separação opcional do
DTO e decisões explícitas sobre os quatro nits. Confirme o novo SHA na descrição
da PR; a revisão original avaliou `c6b7eb5feac1ef0de90901e9568ecf26a95cc082`.

## Objetivo e limites

Entregar parser/schema exatos do contrato oficial, tipos imutáveis, motor puro
V3, registro de 25 critérios, configuração por tenant/versão validada no startup,
DTO público e auditoria congelados. Esta PR **não** entrega endpoints Pix,
providers de produção, seleção condicional DICT, ingestão/persistência bancária,
idempotência, claims ou locks Pix. Verifique os limites contra o código, sem
aceitar comportamento desta etapa incompleto por ter sido listado como futuro.

A PR 1 já foi aprovada e mergeada. Sua Base API, migrations e guardas destrutivas
devem continuar funcionando. Os únicos novos efeitos na API são carregar e
validar o catálogo no startup; não há nova migration ou dependência.

## Requisitos e aceitação

1. Contrato oficial, sem campo extra no cliente; três status e topo público
   exatamente `key/status/metadata`. Centavos inteiros desde parsing de bytes,
   CPF/CNPJ/DV, calendários/offsets, aberturas, ID, IP e hash de todos os campos.
2. Rejeitar duplicatas, NaN/Infinity, NUL e surrogates; entrada inválida falha
   com status/código da biblioteca previsto no guia. Ainda sem tradução HTTP Pix.
3. Motor sem banco/rede/relógio; Context/Policy/Decision e snapshots imutáveis;
   inputs não mudam na avaliação; tenant/histórico isolados e tempo explícito.
4. Regras V3: famílias/tetos/pisos; preliminar usa mesmas regras locais;
   análise, 2FA e execução separados; maturidade/exposição/ocorrência/
   conhecimento/validade corretos. Não promover observação de device a registro.
5. Registro conhecido com parâmetros/schema; configuração completa, tipos
   estritos, pesos não negativos, pares/cortes ordenados, janelas positivas,
   percentuais e famílias válidas; tenant aponta versão existente; inválida
   impede startup. Sem DSL/código arbitrário ou botão para remover pisos.
6. DTO público não vaza documentos/contadores/prontuários; auditoria retém
   política/fingerprint, motivos/pisos/features/evidências. Serialização exata,
   cópias independentes e preservação após mudanças de contexto/política.
7. Equivalência com cenários vigentes, rastreabilidade dos 92 testes da fonte,
   novos testes de fronteira e regressões da API/PostgreSQL da etapa anterior.
   Documentar o que foi realmente exercitado e o que ainda é futuro.

## Contexto e arquivos

Leia `docs/README.md`, `PLANO-IMPLEMENTACAO.md`, `README.md`,
`docs/motor-pix.md` e `docs/validacao-pr2.md`. Fontes vigentes em
`docs/contexto/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md` (seções 3–7 e 9),
`Hackathon Antifraude Pix.pdf` (páginas 19–25), `contrato_pix.py`,
`post_pix_transaction.json`, `politica_v2_simulacao.py` (V3 apesar do nome),
`test_contrato_pix.py`, `test_politica_pix.py`, resultados arquivados e
`REVISAO-INDEPENDENTE-GUIA.md`. Históricos estão identificados no índice;
parâmetros experimentais não são requisitos oficiais ou conformidade.

Código: `src/antifraud/` (contrato, adapter, context, features, rules, engine,
policy/configuration, policies.json, dto), schema em `src/schemas/`,
`src/app.py`, Compose/env, `pytest.ini`, workflow, `tests/unit/`,
`tests/support/pix_scenarios.py`, `tests/reference/verify_pix_reference.py`.

Diferenças deliberadas: consulta DICT necessária sem evidência soma
indisponibilidade uma vez; durações em UTC corrigem overflow e horas decorridas
em horário de verão. Defaults V3 preservados. Janelas limitadas tecnicamente
a um ano para garantir aritmética no domínio de anos 0002–9998. Avalie essas
decisões contra o guia; não presuma que documentá-las basta para estarem corretas.
O simulador só monta contextos nos testes; não é o storage de produção.

## Reprodução

Na raiz de um clone deste repositório, com Docker/Compose v2 disponível:

```bash
git fetch origin
git switch --detach origin/feat/pix-engine
git rev-parse HEAD
# Conferir o SHA com a descrição e headRefOid da PR.
docker compose -p pix-pr2-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr2-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr2-review -f compose.test.yml down -v --remove-orphans
```

Use nome de projeto livre; a stack é descartável, sem portas publicadas.
Não executar resets no banco de uso. Com venv/dependências de dev instaladas:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

Evidência local após correções: 214 testes (83 anteriores + 131 puros),
92 originais separados, 59 observações e 2.000 contextos diferenciais.
Verifique os asserts e a cobertura; esses números não demonstram HTTP/Pix,
transações Pix, concorrência Pix, eficácia ou recuperação de commit.

## Formato dos achados

Para cada achado acionável: prioridade, arquivo/linha, requisito violado,
condição que dispara o bug, reprodução/evidência, impacto e sugestão de
correção. Diferencie bug da etapa, funcionalidade explicitamente futura e
melhoria opcional. Registre incertezas e testes não executados. Se não houver
achados, informe isso com os limites da análise. Não alterar o código, fazer
merge ou iniciar a próxima etapa durante a revisão.
