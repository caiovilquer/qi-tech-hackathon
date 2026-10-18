# Evidências e TDD — PR 3

Base: `1971e51fad28c38ad7e198db78f9ab73a1837b14`, merge aprovado da PR 2.
Checkout limpo, origin SSH existente `caiovilquer/qi-tech-hackathon` conferido,
`git switch main`, `git pull --ff-only origin main`, criação de
`feat/pix-providers` a partir da main atualizada. Nenhum Git na pasta pai.

Validação local em 18/10/2026, macOS ARM64, Python 3.11.15, Docker Desktop e
PostgreSQL **16.15** real. Dados sintéticos; stacks isoladas, sem portas publicadas.
O commit exato em revisão está na descrição da PR e deve coincidir com os checks.

## Baseline e resultados

Antes das mudanças, na main aprovada:

```bash
docker compose -p pix-pr3-baseline -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
```

**214 testes passaram em 4,72 s**, sem skips/xfails. Inclui 83 testes da base
HTTP/PostgreSQL e 131 puros. A referência e fontes existentes foram preservadas.

Depois da implementação:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

- **195 testes puros passaram**, 131 anteriores + 64 desta etapa.
- **92 testes originais passaram** separadamente, sem modificar suas fontes.
- **59 observações** arquivadas e **2.000 contextos** reproduzidos, com Decision
  completa igual à V3 quando os novos inputs bancários/traces estão vazios.
  O conversor diferencial exige vazio para campos ausentes na referência;
  não descarta silenciosamente bank_payments para fingir equivalência.
- O exemplo executável de `providers-pix.md` foi executado, com a saída declarada.

A suíte integral usa a mesma stack de CI:

```bash
docker compose -p pix-pr3-test -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr3-test -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr3-test -f compose.test.yml down -v --remove-orphans
```

**278 testes passaram em 5,00 s**, sem skips/xfails (83 HTTP/PostgreSQL + 195 puros).
Os testes de persistência/concorrência existentes continuam exercitando a
**base e migrations**, não Pix. Checks Linux do SHA publicado complementam a
validação local, sem substituir revisão contra requisitos.

## Sequência TDD e cobertura nova

| Arquivo | Métodos | Comportamento |
|---|---:|---|
| `test_pix_provider_selection.py` | 27 | Versão/observação/conhecimento, expiração/rollback, conflito, escopo, histórico e identidade, maturidade, titularidade, UTC/proveniência |
| `test_pix_provider_adapters.py` | 19 | Contratos dos adapters, dataset imutável, ausência/indisponibilidade, escopo de lookup, budget/retry/cancelamento, IDs e titularidade |
| `test_pix_enrichment.py` | 15 | Context integrado ao motor, C_pre 80/100, gatilho da origem, adverso/restrição conhecidos, memoização da mesma pessoa, dados inválidos, auditoria e budget total |
| `test_pix_provider_connector.py` | 3 | `.25` chega ao requests sem truncamento, configurações inválidas e parsing da variável de ambiente |

Os testes foram escritos antes dos componentes correspondentes:

1. Seleção/identidades: RED inicial por ausência de `providers`; depois,
   tipos e funções concretas tornaram verdes os primeiros 19 métodos.
2. Adapters/budget: RED por módulo ausente, seguido da implementação e 36
   métodos verdes somando a seleção inicial. Timeout usa await real cancelável
   com janela curta; relógio injetado cobre orçamento sem sleeps longos.
3. Enriquecimento: RED por componente ausente; implementação tornou verdes os
   12 métodos iniciais, incluindo composição com motor e histórico bancário.
4. Revisão própria: testes adicionais reproduziram **11 falhas de subcasos e
   um erro** para timeout/configuração e bordas de seleção/identidades. Houve
   RED específico para trace privado ausente, proveniência conflitante,
   duas falhas de titularidade, normalização UTC e ID bancário reutilizado
   entre contas. Correções mínimas, seguidas de suíte verde.

Essas contagens descrevem execuções intermediárias, não quantidade de bugs ou
prova de cobertura exaustiva. Não se afirma que todos os testes novos falharam
por comportamento antes de os módulos existirem. Invariantes já corretos
começaram verdes e foram mantidos.

## Decisões que o revisor deve conferir

- Banco e DICT têm protocolos async normalizados; adapters entregues são
  somente fixtures, sem rede. Não foi inventado endpoint DICT ou adapter real.
- Cancelamento é cooperativo. Nenhum wrapper de requests em uma thread é
  apresentado como garantia de deadline total. O socket timeout da base passou
  a aceitar float positivo; default permanece 5 s para o exemplo de boletos.
- SourceCheck é pré-condição explícita, sem store em memória fingindo claims.
  Initial_result é cobertura verdadeira a persistir; PR 4 fornecerá lease/fencing.
- Cache é material de entrada/saída, sem estado global. Watermark vencido não
  é apagado para aceitar limpeza antiga. Falha/ausência não são zeros válidos.
- Histórico externo é execução, não decisão ou recebimento da API. Só forma
  perfil/mediana após filtros e reconciliação; não cria tentativas/fan-in ou
  reservas de exposição da API. Limites financeiros externos vêm do banco.
- DTO mantém três chaves/status e auditoria privada de fontes/estados; não
  devolve prontuário ou documentos de terceiros ao cliente.

## Integridade e limites

Revisão própria do diff, compilação Python e `git diff --check`.
Schema de runtime e 24 hashes documentais conferidos contra o inventário.
Sem alterar migrations, os 35 testes HTTP originais, guardas destrutivas,
contrato oficial, política default ou arquivos em `docs/contexto/`.

Sem persistência, idempotência/reservas Pix, locks, fencing, ingestão bancária,
HTTP Pix, integração externa real ou promessa de calibração/conformidade.
O estado bancário ausente/vencido fica explícito; a recusa HTTP de contexto
sabidamente vencido é requisito da integração da PR 5, não endpoint desta PR.
Sem implementar antecipadamente as PRs 4/5. Aguardar revisão e aprovação.
