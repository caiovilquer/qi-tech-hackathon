# Contexto para implementação e revisão

Toda a documentação fornecida foi movida da pasta pai para este repositório.
Os 24 arquivos de fonte em [contexto/](contexto/) foram preservados byte a byte,
com os caminhos relativos entre eles. [fontes.md](fontes.md) registra os hashes.
Caches, ambientes virtuais e temporários ficaram fora do Git.

## Ordem de leitura para a PR 1

1. [Prompt de revisão independente](REVISAO-PR1.md): objetivo, critérios,
   arquivos, comandos e formato dos achados.
   Para a nova rodada, leia também o [relatório de correções](CORRECOES-REVISAO-PR1.md).
2. [Plano de implementação](../PLANO-IMPLEMENTACAO.md): limites e gates de
   cada etapa. A PR 1 entrega a base e migrations, sem endpoints Pix.
3. [README do projeto](../README.md), [migrations](migrations.md) e
   [evidências da PR 1](validacao-pr1.md): execução e comportamento entregue.
4. [Guia vigente V3](contexto/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md) e
   [enunciado oficial](<contexto/Hackathon Antifraude Pix.pdf>), páginas 19–25.
   O enunciado define requisitos oficiais; o guia contém decisões do grupo e
   parâmetros experimentais. A maior parte do domínio Pix pertence às próximas PRs.
5. [Revisão independente do guia](contexto/REVISAO-INDEPENDENTE-GUIA.md):
   escopo das correções da referência e limites da validação anterior.

## Fontes vigentes e referência executável

| Material | Função |
|---|---|
| [Guia de implementação V3](contexto/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md) | Especificação de implementação vigente |
| [Hackathon Antifraude Pix](<contexto/Hackathon Antifraude Pix.pdf>) | Contrato e requisitos oficiais |
| [ZIP da Base API](contexto/bootcamp-base-api.zip) | Base obrigatória, sem modificações; importação no commit `602d71c` |
| [contrato_pix.py](contexto/contrato_pix.py) e [schema](contexto/post_pix_transaction.json) | Parsing, normalização e hash de referência |
| [politica_v2_simulacao.py](contexto/politica_v2_simulacao.py) | Conteúdo **V3**, apesar do nome legado; motor puro e simulador sequencial |
| [Testes de contrato](contexto/test_contrato_pix.py) e [de política](contexto/test_politica_pix.py) | 92 testes da referência, separados da suíte HTTP/PostgreSQL |
| [Resultados da simulação](contexto/politica_simulacao_resultados.json) | 59 observações determinísticas; não medem eficácia |
| [Revisão independente](contexto/REVISAO-INDEPENDENTE-GUIA.md) | Evidências e limites da revisão da referência |

Esses arquivos são fontes documentais. Sua presença em `docs/` não significa
que o motor ou os endpoints tenham sido implementados na API. Não importar o
Simulator como repository nem interpretar testes sequenciais como prova de
HTTP, transações, locks ou recuperação de commit.

Com as dependências de desenvolvimento instaladas, executar na raiz do Git:

```bash
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=docs/contexto .venv/bin/python - <<'PY'
import json
from pathlib import Path
from politica_v2_simulacao import scenarios
expected = json.loads(Path('docs/contexto/politica_simulacao_resultados.json').read_text())
assert json.loads(json.dumps(scenarios())) == expected['scenarios']
print('59 observações reproduzidas sem sobrescrever o arquivo de referência')
PY
```

A suíte da API continua no Compose isolado descrito no README. `pytest.ini`
limita a descoberta padrão a `tests/`, para não misturar essas duas evidências.
`docs/contexto/` fica fora do contexto de build Docker e não é dependência de
runtime. Comandos antigos nos originais refletem a organização anterior; para
os caminhos atuais, usar este índice e o README do projeto.

## Material complementar

- [Desafio Antifraude Pix](<contexto/Desafio Antifraude Pix.pdf>): material
  complementar; o enunciado oficial adotado é o arquivo Hackathon acima.
- [Manual de Padrões para Iniciação do Pix](contexto/II_ManualdePadroesparaIniciacaodoPix.pdf):
  referência fornecida, não certificação de conformidade do protótipo.
- [Organização original da Base API](como-o-projeto-e-organizado.md):
  explicação das camadas; os comandos atuais estão no README do projeto.

## Histórico: não usar como especificação vigente

Estes registros explicam decisões anteriores e são citados pelo guia. Em caso
de incompatibilidade, prevalecem os requisitos oficiais e o guia V3.

- [Análise inicial](contexto/ANALISE-ANTIFRAUDE-PIX.md).
- [Ajustes ao enunciado](contexto/AJUSTES-ENUNCIADO-HACKATHON.md).
- [Respostas sobre arquitetura e regras](contexto/RESPOSTAS-DUVIDAS-ARQUITETURA-E-REGRAS.md).
- [Avaliação V2](contexto/AVALIACAO-ANTIFRAUDE-PIX-v2.md).
- [Revisão da avaliação V2](contexto/REVISAO-AVALIACAO-ANTIFRAUDE-PIX-v2.md).
- [Evidências históricas e originais recebidos](contexto/revisao-v2/README.md):
  scripts/resultados antigos reproduzem problemas das versões preservadas,
  não expectativas de aceitação da V3. A saída histórica `.txt` é evidência
  citada por essa revisão; não é um log operacional da API.
