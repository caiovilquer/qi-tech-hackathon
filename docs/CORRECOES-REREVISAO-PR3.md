# Correções da re-revisão independente — PR 3

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/3
Branch: `feat/pix-providers`.
SHA desta revisão: `4e0fdcb9a8964e0023e7ac1b8a5defcd7e676839`.
Novo SHA exato na descrição da PR e nas respostas individuais; comparar
`git diff 4e0fdcb..HEAD` e registrar o HEAD efetivamente revisado.

A revisão confirmou os cinco achados da primeira rodada e identificou dois
novos, ambos procedentes. Os dois foram corrigidos na mesma PR. A observação
opcional de transporte foi adotada; a de relógio UTC foi avaliada e a falha
explícita foi mantida, conforme justificativa abaixo. Não avançar à próxima
etapa sem re-revisão e aprovação explícita.

## Achados e decisões

| Thread | Estado | Correção e evidência |
|---|---|---|
| [4306115749](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306115749), P2-3 | Corrigido | Segurança bancária conhecida e stale exige EnrichmentNeedsRefresh antes de devolver Context, inclusive se já veio vencida ou venceu durante histórico/DICT. Revogação/limite não desaparecem para permitir aprovação. Regressões com revoked, bank_limit_exceeded, registered e cache de revogação vencendo durante history. O teste anterior que só aceitava stale foi atualizado para exigir a interrupção e inspecionar o material parcial. |
| [4306115753](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306115753), P3-4 | Corrigido | Exceção contém EnrichmentProgress imutável com seleção inicial, respostas brutas, seleção/cache_updates, segurança bruta/selecionada e recibos. enrich(..., resume=partial) mantém o resultado inicial e memoização da mesma requisição, inclusive falha. Retomada exige mesmo Pix, providers/versões, permissão, política e objeto Deadline; material de outro tenant/sujeito é recusado antes de I/O. Testes de dois ciclos contam consultas e comprovam resultado inicial preservado. |
| [4306116087](https://github.com/caiovilquer/qi-tech-hackathon/pull/3#discussion_r4306116087), transporte opcional | Adotado | Runner trata ConnectionError, TimeoutError, socket.gaierror e ProviderUnavailable como técnicos. FileNotFoundError, PermissionError, ssl.SSLError e OSError genérico propagam sem retry/outage. SDK-specific errors precisam de tradução explícita e correta no adapter. Testes distinguem configuração de reset/refused/timeout/DNS. |
| Revisão geral, relógio UTC opcional | Avaliado; mantido | Não adotado max(now, valor). O contrato atual falha explicitamente quando o relógio UTC recua; mascarar o recuo manteria um tempo lógico à frente do UTC medido, alterando os cortes de conhecimento/validade sem indicação ao chamador. Orçamento continua monotônico e independente. A escolha está documentada e testada em test_clock_cannot_precede_input_or_go_backwards; não foi apresentada como achado bloqueante pelo revisor. |

## Semântica da continuação

Progress é material local de uma requisição, sem tabela, serviço ou estado
global. Não comprova posse, lease, fencing ou conclusão durável. O coordenador
futuro deve conferir posse antes de persistir os registros normalizados e
controlar até três ciclos no mesmo deadline. O objeto contendo Deadline/clock
não deve ser serializado como registro durável nem reaproveitado em outro request.

initial_result congela a seleção na conclusão da checagem inicial. A seleção
respeita o watermark conhecido: resposta limpa antiga não substitui adverso
mais novo. Se o tempo avança após essa conclusão, fatos atuais são revalidados
separadamente; o resultado inicial não é reescrito ou tratado como limpeza
permanente. Isso permite persistir exatamente a primeira cobertura, inclusive
unavailable, em vez de perdê-la ao sinalizar refresh.

A retomada consulta segurança/histórico atuais, preservando watermarks. Fatos
DICT disponíveis e vigentes são reaproveitados. Falhas/ausências são memoizadas
somente no request, inclusive quando os papéis têm o mesmo documento, sem
virarem cache limpo nem ganharem outro par de retries a cada ciclo. Fato do
destinatário que expirou pode exigir refresh real; essa leitura não reabre a
checagem inicial da origem. Success também devolve progress para revalidação
posterior pelo coordenador. Nenhuma decisão é anexada à exceção parcial.

A reprodução textual de P3-4 com cache do destinatário vencendo antes de ele
ser examinado pode consultar o destinatário imediatamente e não lançar refresh.
A perda de material é, porém, real nos caminhos de refresh existentes. Foi
reproduzida com cache da origem vencendo durante a consulta do destinatário e
com segurança vencendo durante DICT; os dois cenários estão cobertos. Não se
descartou o achado por essa diferença no gatilho da reprodução.

## TDD e evidências

Antes de alterar produção, os sete métodos iniciais em
`tests/unit/test_pix_provider_refresh.py` registraram **sete falhas de asserts
e dois erros**: ausência de refresh, ausência de payload parcial e tratamento
excessivo de erros de configuração. Há subcasos, não nove bugs independentes.
Na revisão própria, dois testes de escopo/memoização registraram uma falha e dois
erros antes do endurecimento do contrato. Outros cinco testes de fronteira
foram adicionados para verificar invariantes da continuação. Um teste adicional reproduziu o reaproveitamento indevido de falha do
destinatário após C_pre subir; corrigido para manter skipped_confidence. Total: **15 métodos
novos**, além da atualização do teste anterior que permitia devolver stale.

Validação local em 18/10/2026, Python 3.11.15, Docker/macOS ARM64:

- **229 testes puros passaram** (214 anteriores +15 desta rodada).
- **312 testes completos passaram em 5,13 s**, sem skips/xfails, incluindo
  **83 HTTP/PostgreSQL da base**, com PostgreSQL **16.15** real.
- Os **15 testes novos passaram isolados no container**.
- **92 testes originais**, **59 observações** e **2.000 contextos** continuam
  iguais à Decision V3 completa, incluindo execução diferencial em container.
- Exemplo executável, integridade das fontes, compilação e git diff --check
  conferidos na rodada. CI do novo SHA deve coincidir com o commit de revisão
  registrado na PR. CI verde não prova ausência de bugs.

PostgreSQL aqui continua validando a base/migrations, sem declarar persistência,
idempotência, ingestão ou concorrência Pix. HTTP Pix e ciclo transacional final
não foram implementados antecipadamente. O motor puro e a política default
não mudaram; a fronteira de enriquecimento impede o Context bancário vencido.

## Reprodução

```bash
# Na raiz do Git, Docker/Compose v2 com daemon disponível:
docker compose -p pix-pr3-review2 -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-pr3-review2 -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/unit/test_pix_provider_refresh.py
docker compose -p pix-pr3-review2 -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-pr3-review2 -f compose.test.yml down -v --remove-orphans
```

```bash
# Com venv de desenvolvimento instalado:
PYTHONPATH=src .venv/bin/python -m unittest tests.unit.test_pix_provider_refresh -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
.venv/bin/python -m unittest discover -s docs/contexto -p 'test_*.py' -q
PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
```

## Prompt para nova revisão

Revise a PR 3, branch feat/pix-providers, no novo SHA exato da descrição e
respostas. Leia este relatório, docs/providers-pix.md, docs/REVISAO-PR3.md e o
guia V3 §§4.2, 5.3 e 8.2. Compare 4e0fdcb..HEAD. Verifique os dois novos
achados e a classificação de transporte, além das regressões anteriores.
Reproduza vencimento de revogação/limite durante consultas e dois ciclos com
cache, outage e mesma pessoa; procure perda de resultado inicial, repetição
de consultas, reset de orçamento, vazamento entre tenants/escopos, rollback
de watermark e aprovação de contexto sabidamente vencido. Testes verdes não
provam ausência de bugs. Para cada achado, informe prioridade, arquivo/linha,
requisito, gatilho e evidência; diferencie requisito futuro de falha desta
etapa. Não altere código, resolva threads, faça merge ou avance etapas.
