# Prompt de revisão independente — PR 3

Revise a PR https://github.com/caiovilquer/qi-tech-hackathon/pull/3
Branch: `feat/pix-providers`
Base: `main`, `1971e51fad28c38ad7e198db78f9ab73a1837b14` (PR 2 aprovada).
SHA: confira o commit exato na descrição e em
`gh pr view 3 --repo caiovilquer/qi-tech-hackathon --json headRefOid --jq .headRefOid`.
Registre o SHA efetivamente revisado. O documento antecede o próprio commit
para evitar referência circular ao hash.

Para a re-revisão, leia `docs/CORRECOES-REVISAO-PR3.md` e revise
`git diff 5e2852c239dbd35695eb8030188268d3ac599beb..HEAD`. Verifique cada um
dos cinco achados e os efeitos das correções, incluindo relógio de resposta,
watermark do initial_result, vencimento durante consultas, namespace de IDs e
exceções. SHA atualizado na descrição da PR e nas respostas individuais.

Procure bugs, regressões, falhas de segurança, inconsistências e testes ausentes.
Não tome testes verdes, TDD ou equivalência com o simulador como prova de
correção. Reproduza o que puder e diferencie incerteza de problema confirmado.

## Objetivo e limites

Entregar contratos BankContextProvider/ReputationProvider, adapters simulados
explícitos/determinísticos, fronteira validada de escopo/tempo/versão, seleção
condicional DICT, histórico bancário reconciliado por identidade e orçamento
cancelável de chamadas. Context final integra o motor puro e auditoria privada.

Não há novo endpoint, migration, persistência/cache global, claim/lease/fencing,
ingestão de eventos, idempotência Pix ou integração com rede externa. SourceCheck
é pré-condição fornecida pelo futuro coordenador; não é reserva em memória.
A arquitetura async dos providers permite cancelamento cooperativo; requests
síncrono não é apresentado como deadline total. Não exigir funcionalidades
explicitamente futuras como se fossem entregues, nem aceitar lacuna real de
uma garantia declarada por estar listada como futura em outro lugar.

## Critérios de aceitação

1. Mesmo contrato normalizado para fixture e futuro adapter real, sem `if mock`
   no motor; dados imutáveis, cobertura/proveniência explícitas e escopo exato de
   tenant/conta/device/destino/owner, operação/permissão e provider/versão.
2. Ausência = not_covered; falha técnica = unavailable; expiração = stale.
   Nenhum deles vira reputação limpa ou cadastro de dispositivo. Quatro estados
   de cobertura não criam quarto status antifraude.
3. Selecionar por observed_at/source_version; available_at filtra conhecimento.
   Entrega antiga não desfaz revogação/adverso conhecido. Watermark vencido não
   autoriza rollback para limpeza antiga. Versões conflitantes falham fechado;
   atualização nova pode cancelar marcação/encerrar relato.
4. Segurança bancária atual sempre consultada para a análise distinta;
   C_pre vem das mesmas regras locais. Remetente resolve checagem inicial em
   OWNED; COMPLETED não reinicia por rejeição/outage/device. Destinatário consulta
   quando C_pre <80, e cache adverso/restrição vigente aplica mesmo com C_pre alto.
5. Escopos de cache incluem tenant/documento/operação/permissão/provider/versão.
   Pessoa em dois papéis pode reaproveitar leitura, inclusive falha, preservando
   ações/estados honestos e contribuição única das regras.
6. Histórico bancário exige ID estável e execução confirmada/coerente; matching
   local só com link explícito, sem heurística valor/horário. Não duplicar
   pagamento nem criar análise/receipt/2FA fictícios. Perfil exige janela,
   ocorrência/conhecimento e maturidade de transação e execução. Não misturar
   titulares da mesma conta silenciosamente.
7. Timeout positivo fracionário, default 250 ms por tentativa, no máximo um retry
   técnico dentro do deadline total compartilhado de 3 s. Cancelamento do caller
   se propaga; resultado tardio é descartado; invalid data não é retry. Identificar
   limites do cancelamento cooperativo, sem prometer matar threads/bloqueio arbitrário.
8. Preservar Base API, contratos/status e motor da PR 2. Sem dependências ou
   infraestrutura extras. Auditoria privada identifica fontes; público não vaza
   dados de terceiros. Evidências distinguem biblioteca, HTTP/base e garantias futuras.

## Contexto e código

Leia `PLANO-IMPLEMENTACAO.md`, `README.md`, `docs/providers-pix.md`,
`docs/validacao-pr3.md`, guia vigente em `docs/contexto/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md`
(§§2, 4, 5, 8.2), enunciado `Hackathon Antifraude Pix.pdf` pp19–25,
`REVISAO-INDEPENDENTE-GUIA.md` e referências Python V3. Índice em docs/README.md.
Requisitos oficiais, decisões do guia e parâmetros experimentais são distintos;
documentação histórica não substitui o guia V3.

Diff principal: `src/providers/{contracts,selection,fixtures,budget,enrichment}.py`,
`src/antifraud/{context,features,dto}.py`, `src/constants.py`,
`src/connectors/rest_connector.py`, quatro testes novos em `tests/unit/`,
`tests/reference/verify_pix_reference.py` e documentação.

Conferir especialmente: profile-only para execuções externas sem análise local,
links explícitos e conflitos entre IDs, proveniência da segurança e DICT,
validade de cache/watermarks, scope de same-person, stale bank que ainda pareça
registered, budget sem reset, retorno/cancelamento fora do prazo e interfaces
para o futuro coordenador sem fingir durabilidade. O diferencial da referência
cobre inputs novos vazios; casos bancários têm invariantes próprios.

## Reprodução

Na raiz de um clone, com Docker/Compose v2 e daemon disponível:

```bash
git fetch origin
git switch --detach origin/feat/pix-providers
git rev-parse HEAD
docker compose -p pix-pr3-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr3-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr3-review -f compose.test.yml down -v --remove-orphans
```

Com venv/dependências de desenvolvimento instaladas:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

Evidência declarada: baseline 214; após revisão 297 (214 puros +83 HTTP/PostgreSQL da
base), 92 originais e diferencial de 59 observações/2.000 contextos default.
Confira asserts/limites, não só totais. O exemplo de docs/providers-pix.md é
executável e não faz rede. Não usar banco do ambiente de uso ou inventar endpoints
para validar mocks. PostgreSQL aqui não comprova persistência/concorrência Pix.

Para cada achado: prioridade, arquivo/linha, requisito violado, gatilho,
reprodução/evidência, impacto e sugestão. Distinga bug desta etapa, requisito
futuro e melhoria opcional. Se não houver achados, diga isso e registre os
limites da revisão. Não altere código, faça merge ou avance à próxima etapa.
