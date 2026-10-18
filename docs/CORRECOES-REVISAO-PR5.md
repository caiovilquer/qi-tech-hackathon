# Correções da revisão independente — etapa 5

PR: https://github.com/caiovilquer/qi-tech-hackathon/pull/6
Branch: `feat/pix-api`. SHA revisado: `6acd2e72838d3c88d1f155c443f04e4683bbc897`.
O SHA corrigido e os links de CI ficam na descrição da PR e nas respostas aos oito
comentários. Comparar `git diff 6acd2e7..HEAD`; registrar o HEAD efetivamente lido.
A re-revisão do SHA `f3401f4` e sua segunda rodada de correções estão em
[CORRECOES-REREVISAO-PR5.md](CORRECOES-REREVISAO-PR5.md).

A revisão trouxe oito itens inline e quatro lacunas adicionais de testes no
relatório geral. Todos foram verificados com código/guia e tratados nesta PR.
As ressalvas de reprodução dos itens 7 e 8 estão explícitas abaixo; não se
interpreta a concordância parcial como prova de que todo o diagnóstico era exato.
Não há pendência bloqueante conhecida nem transferência de bug para a etapa 6.

## Achados e decisões

| Item / comentário GitHub | Estado | Correção e evidência |
|---|---|---|
| 1 / 4306602667 — condições de reconciliação viravam retry 503 infinito | Corrigido | `OwnershipConflict` e `ReconciliationRequired` viram **409 QIT002010**, com orientação de reconciliação institucional antes de retry. Não se mistura titularidade nem se inventa vínculo bancário. O dono expira suas duas leases por CAS de tenant/ID/hash/owner/fence, preservando hash e recusando liberar reservas de outro dono. Dois POSTs consecutivos retornam 409 sem esperar 10 s; overlap reparado por vínculo explícito permite continuar o mesmo corpo imediatamente. Hash diferente continua QIT002002. |
| 2 / 4306602669 — amplificação por receipts anônimos | Corrigido | Autenticação inválida retorna 403 **sem storage/pool**. Auditoria anônima é o logger existente, sem corpo ou credencial; receipts SQL são de POST autenticado. Flood de 40 POST/GET com 20 clientes tem zero chamadas a receive. |
| 3 / 4306602671 — mistura dos relógios app/DB | Corrigido | O coordenador ancora UTC em `clock_timestamp()` do PostgreSQL e avança por elapsed monotônico (`DatabaseClock`), explicitamente injetado no Enricher. Contexto, cobertura sintética e recebimentos usam essa escala; decisão final/leases continuam no DB. Não há tolerância que redate evidência de fonte. Skews locais de −1 dia, +1 s e +1 dia concluem 201 com inicial contemporânea ao DB. Guarda contra cobertura realmente futura continua rejeitando. |
| 4 / 4306602678 — recuperação de 23505 tentava outro claim | Corrigido | Após rollback da constraint exata, `resolve_duplicate` lê a análise concluída por INNER JOIN, sem adquirir outro dono. Hash igual: 200; diferente: 409; Pix sem análise: 503. Preflight de claim reconhece análise já durável e mantém replay sem providers, respeitando o hash permanente. Três cenários provocam **23505 real** com writer independente inserindo antes do lock final. |
| 5 / 4306602681 — timeouts Pix afetavam Base API | Corrigido | `SessionLocal/engine` da base recuperam parâmetros originais (checkout padrão 30 s, sem connect_timeout imposto). `PixSessionLocal/pix_engine` têm pool próprio, checkout 100 ms e connect_timeout 1 s. Com 15 conexões Base ocupadas, o Pix pelo factory real conclui; a 16ª solicitação Base espera mais de 100 ms e conclui 200 quando uma conexão é liberada. |
| 6 / 4306602683 — auditoria encobria 400/422 | Corrigido | Erro conhecido de corpo/contrato mantém 400/413/422 mesmo se receive/identify falhar; falha de auditoria é logada. Corpo válido sem receipt confiável continua 503 antes de claims/providers. Testes de receive/identify falhando e PostgreSQL realmente inacessível, sem inventar persistência. ID extraído de inválido exclui `/?#`, controles e `.`/`..`. |
| 7 / 4306602688 — reingestão de execução conhecida | Corrigido, alcance ajustado | **Não ocorria em toda requisição**: `reconcile_payments` já preservava o evento local quando presente na primeira leitura. Reproduziu-se quando a ingestão chega entre o enriquecimento e `_persist`. O diff agora usa `(payer, pix_id, provider:event_id)` e confere estado/ocorrência, ignorando somente metadado de conhecimento. Nenhuma chamada extra a ingest nesse interleaving nem nas solicitações seguintes. |
| 8 / 4306602690 — injeção no logger | Corrigido, reprodução ajustada | O probe `%0A` não injeta linha nesta versão: a construção de `Request.url` remove CR/LF. **U+2028** no segmento foi preservado e reproduziu a falha. Path/query de entrada e path de saída agora são escapados por JSON (`ensure_ascii=True`), protegendo Pix e Sample Entity no middleware comum. Teste CR/LF começou verde; U+2028 registrou RED antes do ajuste. |

### Refinamentos materiais de contrato

QIT002010 é um erro novo de **integração/conciliação**, não outro status antifraude
nem rejeição fictícia. O guia vigente não atribui um código específico a esses
conflitos; o comportamento anterior usava indevidamente a orientação de retry de
falha técnica. Se corrigir o payload, usar outro ID: a reserva original continua
com o primeiro hash. Reatribuição de titularidade não ganha API administrativa;
a instituição deve reconciliar seus dados sem apagar o histórico existente.

Em erros já conhecidos do cliente, auditoria indisponível não muda o diagnóstico
para 503. Essa é uma precisão da matriz HTTP: o 503 de banco indisponível aplica-se
a entrada válida cujo resultado confiável não pode ser persistido/lido. Se o prazo
acabar **antes** de ser possível ler/validar o corpo, permanece 503; não se promete
validar um stream incompleto fora dos 3 s. Nenhum sucesso novo ocorre sem receipt
confirmado e commit da análise. Anonymous requests não adquirem conexão.

O caminho 23505 é defensivo: o protocolo normal de claims/locks impede dois
coordenadores de chegarem ao INSERT concorrente do mesmo ID. O teste injeta um
writer externo ao protocolo, respeitando constraints e gravando uma análise
completa antes dos locks finais. Não se trata toda unique como replay nem se
promete consertar automaticamente um banco modificado fora do protocolo. Sem
análise concluída não há DTO confiável para retornar.

Os pools separados podem abrir até **30 conexões por processo** (2 × [5 + 10 de
overflow]); dimensionar workers com a capacidade PostgreSQL. Não se introduziu
outro banco, serviço ou dependência. A observação sobre socket TCP parado permanece:
a espera HTTP é cancelável, mas uma thread pode esperar o timeout TCP do sistema;
não se afirma que cancelar a coroutine mata essa thread.

## Testes adicionais do relatório geral

- `statement_timeout` e `lock_timeout` lidos no PostgreSQL em transações de leitura
  e escrita, limitados pelo orçamento restante; duas leituras mostram redução
  efetiva, e lock customizado de 37 ms é preservado quando menor.
- `Content-Length` não numérico: guarda do Resource retorna 400 QIT000001 e
  finaliza receipt. Teste direto ASGI, pois o Uvicorn rejeita esse header antes da
  aplicação; não se atribui o envelope QI à rejeição do parser HTTP do servidor.
- Dois tenants passam por `PIX_TENANTS`, arquivo de políticas, `create_app()` e
  HTTP, sem sobrescrever o cadastro em app.state; mesmo ID gera análises isoladas
  e ISPBs privados distintos. Credencial antiga não configurada recebe 403.
- Saturação de 15 conexões Base e 16ª solicitação exercitada com SQL e HTTP reais,
  além do flood sem credencial. Não é benchmark de capacidade de produção.
- Liberação das leases não expira outra posse/fence; cobertura de fonte realmente
  futura continua recusada. Esses dois casos começaram verdes, como proteção das
  invariantes do novo caminho e do guard existente.

## TDD e validação

Baseline do SHA revisado: **413 passaram em 31,76 s**, PostgreSQL real.
Primeira rodada de regressões: **12 falhas / 1 verde**; após corrigir o escopo do
skew para simular também o default do Enricher, os três skews têm RED no SHA
revisado. Rodada do vencedor/logger: **3 falhas / 2 verdes** (200 e 409 da unique
nomeada e U+2028 falharam; Pix órfão 503 e CR/LF já estavam corretos).

Uma asserção inicial de logger esperava `\\n` na saída, mas Request.url já retirava
a quebra: esse RED não provava injeção. Foi substituído pelo caso U+2028 e pela
verificação separada do CR/LF. Testes verdes não são prova de ausência de bugs.

Os testes estão em `tests/integration/test_pix_api_review.py`; o teste já existente
de PostgreSQL inacessível ganhou corpos inválidos, o de autenticação passou a
exigir zero receipts anônimos e o harness ganhou opção de preservar configurações
carregadas pelo startup. Nenhuma guarda antiga de isolamento/atomicidade foi retirada.

Validação local final em **18/10/2026**: **436 testes passaram em 36,50 s**,
build completa sem mounts de código (`pix-pr5-review-submit`); **23 regressões
passaram isoladas em 6,28 s**, sem skips/xfails e com somente o warning original
do declarative_base. PostgreSQL **16.15** real, Python **3.11.17**, Docker **29.8.2**
e Compose **5.6.0**, macOS ARM64. **92 testes originais em 0,130 s**, **59 observações
e 2.000 contextos** equivalentes à Decision V3 completa. **101 arquivos Python**
compilados em memória; 24 hashes documentais, diff e migrations/schema preservados
conferidos. Stacks descartáveis próprias removidas; ambiente `pix-pr1` preservado.
Logs `../tmp/pr6-review-*.log`, fora do Git. Sem merge, resolução automática de
threads ou avanço da etapa; aguardar re-revisão e aprovação explícita.

Reprodução, na raiz deste Git, com Docker ativo:

```bash
docker compose -p pix-api-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-api-review -f compose.test.yml run --rm --no-deps tests python -m pytest -q tests/integration/test_pix_api_review.py
docker compose -p pix-api-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-api-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python -m unittest discover -s /reference -p 'test_*.py' -q
docker compose -p pix-api-review -f compose.test.yml down -v --remove-orphans
```

## Prompt para re-revisão

Revise a PR #6 no novo SHA da descrição, comparando `6acd2e7..HEAD`. Leia este
relatório, docs/api-pix.md e docs/validacao-pr5.md. Verifique especialmente:
409 QIT002010 e liberação fenced das duas leases sem perder hash; nenhuma ida ao
DB para anônimos; relógio UTC ancorado no PostgreSQL sem aceitar fonte futura;
23505 real lendo apenas análise concluída; pools separados preservando Base;
400/422 não encobertos por auditoria; identidade de execução no interleaving;
escape de controles/Unicode nos logs; e os quatro testes adicionais do relatório.
Procure bugs/regressões/falhas de segurança com prioridade, arquivo/linha, gatilho
e evidência. Não concorde por haver testes verdes. Não resolva threads, faça
merge ou avance para a etapa 6; aguardar aprovação explícita do usuário.
