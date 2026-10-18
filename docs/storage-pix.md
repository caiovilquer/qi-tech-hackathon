# Persistência interna Pix — etapa 4

Esta etapa entrega APIs Python internas com PostgreSQL real, antes das rotas
Pix. Não entrega POST/GET HTTP, providers remotos, execução financeira ou 2FA.
O enunciado oficial exige Base API e histórico; leases/fencing, eventos,
locks e semântica de erros são decisões do guia V3, não exigências atribuídas
indevidamente ao PDF. Política numérica permanece experimental e inalterada.

## Camadas e unidades de transação

- `models/pix.py`: mappings SQLAlchemy Core de consulta/escrita, integrados
  ao metadata da Base API. Não geram DDL: migrations são a fonte de constraints,
  PKs/FKs/índices/triggers. Não chamar create_all como mecanismo de evolução.
- `repositories/pix_repository.py`: recebe contexto com db_session, como na
  base. Só lê/escreve SQL parametrizado; não faz commit/rollback ou rede.
- `controllers/pix_storage.py`: serviço interno com Session factory injetada.
  Coordena transações curtas de claims/checagem inicial/receipts e transação
  final em READ COMMITTED explícito. Faz flush/commit dentro do tratamento de
  erros, rollback e close em qualquer falha. Não usa contextvar fora do request.
- `dtos/pix_storage.py`: tickets/outcomes imutáveis e eventos de entrada tipados;
  `utils/pix_storage_codec.py`: JSON versionado com whitelist de tipos. Sem
  pickle/eval, SQL dinâmico do cliente ou desserialização de classes arbitrárias.
  Conteúdo armazenado malformado é sempre `ValueError`, nunca erro de tipo solto.

Receber o DTO de `work.complete` dentro do with é valor interno. O chamador
só pode responder sucesso **depois** de sair de `storage.analysis` sem erro,
quando o commit foi confirmado. Erro de flush/commit propaga; resultado de
commit desconhecido deve ser resolvido pela leitura/retry do mesmo ID/hash
na integração seguinte. Não há tentativa de adivinhar commit remoto neste serviço.

## Reservas e checagem inicial

`storage.claim(tenant, external_id, payload_hash, lock_timeout_ms=1000)` confirma
uma reserva curta antes de providers. Retorna ClaimTicket (dono, fencing e
`lease_until` no relógio PostgreSQL), Replay com DTO imutável concluído, Busy para
outro dono ativo ou HashConflict QIT002002 para hash diferente, inclusive em voo.
Uma leitura sem lock responde HashConflict/Replay de imediato mesmo com a linha da
reserva travada pelo dono em transação final; esperar por essa linha até
`lock_timeout_ms` (SQLSTATE 55P03) também é Busy, não falha técnica. Hash e ID são
permanentes. GET interno não retorna reserva sem análise. Lease vencida permite
takeover com fence crescente; o dono anterior não consegue iniciar/concluir a
transação final. Não há renovação de lease: a transação final deve começar dentro
dos 10 s informados em `lease_until`.

`claim_initial(operation_ticket, AccountKey)` exige posse atual da operação,
valida titularidade por tenant/branch/account e reserva uma lease/token própria
da origem. Ausência é unclaimed; outro dono ativo, inclusive a espera pela linha
até `lock_timeout_ms`, gera Busy, não completed. Retorna InitialTicket (com
`lease_until`) ou None quando a checagem já está completed. Mudança de titular
exige reconciliação explícita, sem misturar histórico/contadores: `pix_account` é
imutável por trigger e nesta etapa não existe API nem migration para reatribuir
uma conta; esse caminho operacional fica para uma etapa posterior.

`complete_initial(initial_ticket, selected_reputation_record)` trava/verifica
**ambos** os tokens, donos e leases. Persiste exatamente o resultado tipado
selecionado na conclusão, inclusive unavailable/not_covered/stale, e fatos
cacheáveis no mesmo commit. Completed é imutável e sobrevive à falha/rejeição
da análise. Não transformar esse estado em declaração de pessoa limpa: os
fatos têm validade própria. Troca de device não reinicia a checagem. Se o cache
já guarda a mesma revisão (observed_at/source_version) com fato ou proveniência
diferentes, `RecordConflict` aborta a conclusão inteira: a reserva permanece com o
dono para uma nova tentativa corrigida, ou vence para takeover. Não se inventa
indisponibilidade nem se grava um fato conflitante para destravar a conta.

Leases são 10 s no relógio UTC do PostgreSQL. Não há espera/polling infinito,
renovação implícita ou sucesso de dono substituído. A API futura deve tratar
Busy dentro do deadline da requisição, e repetir mesmo ID/corpo ao retornar
503. Esta etapa não implementa ainda esse loop HTTP ou takeover por processo
morto de ponta a ponta; testes de fencing expiram leases no banco descartável.

## Locks, leitura e gravação final

`with storage.analysis(ticket, pix) as work` abre READ COMMITTED, adquire
advisory locks transacionais de origem e destino e valida novamente claim,
lease/fence e titularidade. As chaves são namespaced, derivadas de SHA-256,
convertidas para int64 assinado; ordenam-se os IDs numéricos efetivos e removem-se
duplicados. Colisão só reduz paralelismo. Lock timeout é 1 s, configurável para
menos pela futura integração. O statement timeout de 3 s é por statement,
**não** prova do deadline total HTTP; esse orçamento pertence à etapa 5.
Leituras (`read`) usam READ COMMITTED e os mesmos limites de lock/statement.

`work.context(pix, decision_at, policy)` lê o histórico da própria conta na janela
comportamental, as tentativas ao mesmo destino apenas na janela de 24 h do fan-in
(`FAN_IN_WINDOW` do motor, única parte do histórico de terceiros que as features
usam) e recebimentos da própria conta na janela de decisão. SQL filtra tenant,
conta/destino e tempos; não carrega toda a instituição nem trunca arbitrariamente
contagens/medianas. Eventos têm ocorrência e recebimento
separados e só entram quando ambos já eram conhecidos. Tempos sem timezone
são recusados antes das consultas. Ordenação é determinística.

`work.complete(normalized, context, policy, receipt_id=..., raw_body=...)` exige
checagem inicial durável, payload/hash/Pix iguais à reserva e decision_at
capturado após os locks com `work.now()`, o relógio UTC do PostgreSQL. O relógio
da aplicação usado pelo Enricher não serve para esse campo: um decision_at
anterior aos locks ou à frente do relógio do banco é recusado com mensagens
distintas. Relê o contexto local e compara histórico, execuções e autenticações
como multiconjuntos: fato omitido ou desatualizado é "rebuild context under the
held locks"; execução presente no Context mas ausente do banco é "not persisted
locally", porque cada fato usado na decisão precisa existir em `bank_event`.
Segurança sabidamente vencida também é recusada.
O motor puro calcula a decisão; grava Pix, análise/DTO e snapshot imutáveis,
conclui o claim e finaliza o receipt no mesmo commit. Falhar após inserts ou
no próprio commit não deixa meia análise ou claim completed.

Protocolo esperado do coordenador da etapa 5, por ciclo:

1. Fora de locks: `base = repo.context(pix, now, policy)` e `links = repo.bank_links(payer)`;
   `Enricher.enrich(base, ..., links=links)`.
2. Persistir fatos externos verificados em transações curtas: `put_snapshots` para
   `cache_updates`, `link_bank_operation` e `ingest` para cada execução bancária
   vinculada a uma operação local. A reconciliação da etapa 3 reconhece uma
   execução já conhecida localmente pelo mesmo provider/ID e não a duplica; o
   instante de recebimento local é metadado de conhecimento, não parte do fato.
3. Sob locks: `now = work.now()`, `work.complete(normalized,
   replace(enriched.context, decision_at=now), policy)`. A substituição do
   decision_at revalida ações `cache` em `Context`; evidência vencida nesse
   instante exige novo ciclo, até três ciclos/3 s.

Não entregar Context pré-lock como se fosse atual nem carregar no Context uma
execução bancária que não foi ingerida; a biblioteca recusa ambos, mas não
executa o coordenador HTTP completo nesta etapa.

## Receipts, hash e análise original

`receive` grava apenas request/tenant quando autenticado, ID/hash quando
extraíveis e instante UTC. Não recebe token, headers ou credenciais. Resultado
pode ser finalizado sem análise para erro/replay, e replay pode apontar para
a análise original do mesmo tenant. ID/hash de um receipt de outra operação
não são substituídos para encaixar uma análise. Pix.received_at usa o receipt
original quando fornecido; fallback interno é o instante da gravação.

Dinheiro continua inteiro em centavos até BIGINT. Score usa NUMERIC com
CHECK de inteiro não negativo, sem impor um teto int32 ausente da política. Canonical payload e DTO/audit
são congelados; raw_body é opcional, limitado a 64 KiB e revalidado contra o
hash oficial, sem headers. IP inválido permanece null no canônico e bruto
na auditoria, como na etapa 2. Reconstruir o Pix do histórico usa as colunas
tipadas; não repassa o payload canônico por um parser de requisição nova.

`get(tenant, external_id)` e Replay retornam o DTO gravado. Eventos, nova
política, 2FA ou execução não reescrevem decisão, score, timestamp ou key.
Triggers bloqueiam mutações de Pix/análise, eventos aceitos, vínculos e
reservas concluídas. Não há quarto status antifraude.

## Eventos bancários e vínculos internos

`bind_challenge` exige análise original two_factor_required, tenant/ID, `key`
(UUID obrigatório), fingerprint financeiro exato (hex de 64) e challenge ID
explícito emitido pelo banco. Provider fora do padrão, key ausente/inválida,
fingerprint malformado ou identificador com caractere de controle são InvalidEvent
QIT002007 antes de qualquer SQL. Um vínculo por análise; outro Pix/valor/destino
não pode reaproveitá-lo; repetir o mesmo vínculo é replay.
`link_bank_operation` faz ligação por provider/ID bancário, sem heurística de
valor/horário. `bank_links(AccountKey)` retorna todos os vínculos da conta,
sem omitir aliases por paginação silenciosa.

`ingest(EventInput)` é chamada interna para adapter confiável; nenhum endpoint
público aceita esses fatos. Identidade é tenant/provider/event_id; conteúdo
igual é replay e diferente gera EventConflict QIT002006. Locks incluem origem,
destino e identidade do evento/vínculo, em ordem numérica global. Hash ignora
instante local de recebimento e normaliza offset equivalente em UTC.

Execução só aceita executed/failed/cancelled terminais, vinculados à operação,
análise e fingerprint. Não muda estado terminal nem desloca a ocorrência de
um pagamento já confirmado. Execução é fato financeiro: pode ser registrada
sem promover a decisão original a approved, inclusive se o banco informou
execução de uma análise originalmente rejeitada. Não é autorização para executar.

Autenticação aceita passed, failed não terminal/terminal e expired terminal,
sempre com desafio exato. Entrega antiga não terminal pode chegar depois de
sucesso; ordem/versionamento da fonte impede reabrir ou contradizer terminal.
Na entrada do motor, expired vira failed terminal para a mesma análise/desafio.
Dois eventos distintos do mesmo provider com occurred_at e source_version iguais e
estados diferentes são recusados como revisão conflitante: uma fonte sem
versionamento precisa diferenciar seus eventos pelo instante.
Passar 2FA não cadastra device. Falha terminal só libera exposição vinculada;
execução financeira continua em trilha independente.

Evento desconhecido, vínculo incompatível, revisão conflitante ou tempo futuro
gera InvalidEvent QIT002007, sem alterar fato aceito. Optou-se por recusar
operações desconhecidas; estacioná-las para reconciliação seria opcional no guia
e não foi introduzido serviço/fila. Segurança atual entra por BankRecord
normalizado, com escopo/versão próprios, persistido como snapshot de provider;
observação do payload ou evento de 2FA não vira evento de cadastro autorizado.

## Snapshots de providers

`put_snapshots` recebe BankRecord/ReputationRecord da etapa 3. Cache persistido
inclui tenant, documento/conta/device/destino quando aplicável, operação,
permissão, provider e versão. Revisão é observed_at/source_version; available_at
e received_at local controlam conhecimento. Consulta anterior à ingestão local
não recebe um fato só porque ele descrevia uma observação antiga.

Mantêm-se revisões anteriores e watermark expirado. Seleção do mais novo
conhecido não filtra valid_until para voltar à limpeza antiga: retorna stale.
Mesma revisão com fato conflitante gera RecordConflict e rollback; duplicatas
coerentes preservam primeiro conhecimento e metadado conservador stale.
Lotes são ordenados por tenant/chave/observação/versão para evitar inversão
de locks de linhas. Não renova validade. Falhas/ausências não são cache limpo; o resultado inicial
inclusive indisponível permanece durável em source_initial_check.

Cache usa somente os registros normalizados, não EnrichmentProgress com
Deadline/callable. Persistência de progresso e uso de SourceCheck/resume devem
ser conectados pelo coordenador da etapa 5, comprovando posse/fencing.

## Correções da revisão independente

Os achados da revisão do SHA `7fead97`, as decisões sobre os itens opcionais e as
regressões correspondentes estão em
[CORRECOES-REVISAO-PR4.md](CORRECOES-REVISAO-PR4.md).
