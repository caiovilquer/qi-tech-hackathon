# API Pix — etapa 5

Os endpoints oficiais estão integrados ao motor puro, providers e PostgreSQL:
`POST /pix/transaction` e `GET /pix/transaction/{id}`. Contrato/centavos/três
status vêm do enunciado; normalização, erros, leases, locks e orçamento são
escolhas do guia V3. Pesos/cortes continuam experimentais, sem calibração ou
conformidade comprovada. Não há execução financeira, desafio 2FA, webhook
público nem integração externa real com DICT/banco.

## Configuração e autenticação

O header `INTERNAL-TOKEN` resolve a instituição. `PIX_TENANTS` é JSON de servidor:

```json
{"demo":{"token":"default_token","participant":"00000000"}}
```

Valores acima são fictícios. Sem `PIX_TENANTS`, usa-se `demo`, o valor de
`INTERNAL_TOKEN` e ISPB sintético `00000000`. Para outro tenant, definir também
sua política em `PIX_POLICY_FILE`. Credenciais repetidas, ISPB malformado, tenant
sem política, JSON duplicado ou configuração inválida impedem o startup.
Nenhum campo do cliente escolhe tenant ou ISPB de origem. A identidade do tenant
não deve ser reaproveitada para outra instituição. O ISPB de origem fica no
snapshot privado; o canônico e o DTO oficial mantêm o contrato fornecido.

Sample entity preserva sua autenticação original. Credencial Pix desconhecida
retorna 403 `QIT000002`. Receipts anônimos não guardam token/corpo; falha de banco
nessa auditoria é logada e não transforma uma autenticação recusada em acesso.

`create_app()` monta `PixController(PixStorage(SessionLocal), bank, reputation)`.
Os providers padrão são `FixtureBankProvider(fixture-bank/v1)` e
`FixtureReputationProvider(fixture-dict/v1)`, com dataset explicitamente vazio:
retornam **not_covered**, nunca autorização de dispositivo ou reputação limpa.
O motor ainda calcula a decisão, sem status fixo.

`PIX_FIXTURE_FILE` aceita um arquivo local de servidor, montado somente para
leitura no container, com arrays `bank`, `history` e `reputation`. Elementos usam
o codec normalizado `pack` de `utils/pix_storage_codec.py` (BankRecord,
BankPayment e ReputationRecord), com os providers/versões acima. O loader usa
whitelist, valida os contratos e recusa campos/duplicatas/tipos desconhecidos;
não executa código do arquivo nem aceita datasets enviados por HTTP. Sem um
arquivo configurado, nenhuma fixture de teste é importada pela aplicação.
Adapters remotos futuros implementam os mesmos protocolos assíncronos.

## Contrato HTTP

| Situação | HTTP e código |
|---|---|
| Nova análise, approved/two_factor_required/rejected | 201 após commit confirmado |
| Mesmo tenant/ID/hash | 200 com DTO original; sem providers |
| Mesmo tenant/ID, hash diferente, inclusive em voo | 409 QIT002002 |
| GET por ID externo concluído | 200 com DTO original |
| GET sem análise no tenant, inclusive apenas claim | 404 QIT002001 |
| JSON/schema/tipos/formatos inválidos | 400 QIT000001 |
| DV, data semântica, abertura após transação | 422 QIT002003/4/5 |
| Mais de 64 KiB, inclusive corpo chunked | 413 QIT002009 |
| Reserva ocupada até o deadline, dado que não estabiliza, erro SQL/commit/DB | 503 QIT002008; repetir mesmo ID e corpo |

Erros usam o envelope da Base API `title/description/translation/code`, com
mensagens sem payload, documento ou credencial. Sucesso tem apenas
`key/status/metadata`. GET usa o `id` externo, codificado como segmento de URL,
e não o UUID `key`. Novos eventos/políticas nunca reescrevem a análise original.
Não há um quarto status antifraude para processamento ou erro técnico.

`PixResource` lê incrementalmente até 65.536 bytes antes de chamar
`parse_payload_json`/`normalize_payload`; não usa `request.json`, Pydantic/dict ou float para dinheiro.
Limite exato é aceito; o corpo bruto não é armazenado. O canônico e a auditoria
preservam todos os campos oficiais e a qualidade de IP. Receipts mínimos são
persistidos antes de ler/validar o corpo, depois associados ao ID/hash extraídos;
inválidos/replays não viram novas análises nem tentativas pontuadas.

## Protocolo do coordenador

1. Autenticar e receber receipt. Normalizar e identificar receipt; reservar
   tenant/ID/hash em transação curta **antes de providers**. Replay/conflito
   encerra aqui. Mesmo hash ocupado espera dentro do prazo; hash diferente não
   espera pela finalização do dono. A reserva mantém o hash após falha técnica.
2. Reivindicar a inicial da origem com token/lease próprios, verificando a posse
   da operação. Outro dono ativo não é tratado como inicial concluída. Aguardar
   completed ou responder 503; takeover só depois de expirar a lease.
3. Ler contexto/cache/vínculos e enriquecer **fora dos locks das contas**. O
   callback `on_initial` grava o resultado selecionado do remetente assim que
   existe, inclusive unavailable/not_covered, antes de uma possível falha do
   destinatário. A continuação é local à requisição; não prova posse durável.
4. Persistir snapshots normalizados e ingerir execuções já ligadas explicitamente
   a análises locais. Não inferir vínculo por valor/destino/horário. Ocorrência e
   conhecimento futuros não ganham um recebimento local antecipado.
5. Em READ COMMITTED, travar origem e destino na ordem numérica dos advisory IDs,
   verificar claim/fencing, capturar `work.now()` no PostgreSQL e reconstruir todos
   os fatos locais. Reconciliar novamente as identidades bancárias: um vínculo
   criado desde o enriquecimento não pode duplicar o mesmo pagamento.
6. Reler snapshots por escopo/versão/conhecimento e recalcular `C_pre`. Mudança ou
   vencimento de segurança/cache, nova consulta necessária do destinatário ou
   execução recém-vinculada ainda não ingerida libera a transação para novo ciclo.
   Retomar progresso sem repetir a política inicial do remetente. No máximo três
   ciclos, sujeitos ao mesmo deadline. Sem estabilização, 503 sem análise parcial.
7. Avaliar o motor e confirmar Pix/análise/DTO/claim/receipt em um único commit.
   Só então 201. Apenas SQLSTATE 23505 com `uq_pix_tenant_external_id` permite
   tentar ler um vencedor como replay/conflito; outras constraints não são replay.

Pagamentos importados não viram receipts/análises fictícios. Os fatos completos
usados ficam congelados em `snapshot.bank_payments`, além de seus identificadores
nas features. Dados fora da janela comportamental não exigem carregar análises
locais antigas apenas para descartá-las no cálculo; decisões puras são preservadas.

## Deadline e falhas

O orçamento monotônico de **3 s** começa no middleware Pix e inclui corpo,
threadpool, SQL, espera por reservas e providers. Reservam-se 100 ms desse mesmo
prazo para finalizar receipts de erro. Providers têm até 250 ms por tentativa e
um retry técnico; não há retry de erro de dados/configuração. Ausência/falha de
provider vira cobertura verdadeira e incerteza, sem inventar dados limpos.
Segurança sabidamente vencida precisa ser renovada ou a análise falha com 503.

SQL síncrono vai ao threadpool. O orçamento acompanha o worker por ContextVar;
timeouts PostgreSQL são recalculados antes de cada statement, flush e commit.
Lock timeout é no máximo 1 s e nunca supera o prazo restante. Pool de conexões
espera até 100 ms e novas conexões usam connect_timeout de 1 s. A espera HTTP é
cancelável; worker que começou tarde não inicia SQL e não confirma nova transação
com orçamento vencido. Não se afirma que cancelar uma coroutine mata uma thread.

Se COMMIT já foi enviado e a confirmação se perde/atrasa, 503 é resultado
**desconhecido**, não prova de rollback. O cliente repete o mesmo ID/corpo;
claim/hash permanentes e DTO durável resolvem a ambiguidade. O receipt atômico
pode registrar o resultado confirmado no banco mesmo quando o cliente recebeu
503 por perda da confirmação. Falha ao gravar/finalizar receipt é logada; não se
promete auditoria durável com PostgreSQL indisponível. Replay só responde sucesso
após sua própria finalização de receipt ter sido confirmada.

Leases são 10 s no relógio PostgreSQL, sem renovação. Elas sobrevivem a crashes;
retentativa antes de expirar pode retornar 503. Não há atomicidade com uma fonte
bancária externa que mude depois do snapshot, nem garantia de tempo real diante
de suspensão do processo/host. O banco chamador mantém os controles finais antes
de executar qualquer operação. Eventos seguem a API interna confiável da etapa 4.

Validação/TDD: [validacao-pr5.md](validacao-pr5.md). Revisão:
[REVISAO-PR5.md](REVISAO-PR5.md). A etapa 6 permanece para aceitação/demonstração e
documentação final; não recebe bugs obrigatórios conhecidos desta etapa.
