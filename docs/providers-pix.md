# Providers, histórico e enriquecimento Pix — PR 3

A PR entrega uma biblioteca integrada ao motor puro, antes da exposição HTTP.
Não adiciona serviço, fila, Redis, dependência, migration ou endpoint. O PDF
oficial define o contrato Pix; escopo, temporalidade, consulta condicional e
coordenação são decisões do guia V3. Pesos, TTL de referência, 250 ms e 3 s são
parâmetros experimentais, sem calibração/conformidade comprovada.

## Contratos e adapters

`src/providers/contracts.py` declara `BankContextProvider` e
`ReputationProvider`. Ambos identificam `ProviderIdentity(name, version)`.
Os métodos são **async**, recebem `as_of` e `timeout_seconds` fracionário e
retornam dados normalizados/imutáveis. Fixture e futuro adapter remoto usam
a mesma assinatura; não há branch de regra `if mock` ou adapter real fictício.
`as_of` é o instante de despacho/corte das fixtures, **não** uma leitura
histórica exigida do DICT real. Um adapter remoto informa tempos reais de
observação/conhecimento, inclusive posteriores ao despacho. O `Enricher`
usa relógio UTC injetável (`clock`, default servidor), captura o recebimento
após cada operação e define `Context.decision_at` depois das respostas.
Testes/fixtures históricos devem injetar seu relógio, em vez de usar o relógio
atual para simular uma decisão passada. Regressão de relógio falha explicitamente.

- Banco: `security(SecurityScope, as_of, timeout_seconds=...)` e
  `payments(AccountKey, as_of, timeout_seconds=...)`, métodos separados.
- Reputação: `reputation(ReputationKey, as_of, timeout_seconds=...)`.
- `SecurityScope` inclui pagador/tenant, device observado, conta destinatária
  e documento do titular de destino. Não emprestar confiança a outro device,
  conta, titular ou tenant.
- `ReputationKey` inclui tenant, documento normalizado/DV, operação
  `person_reputation`, escopo de permissão, provider e versão. Papel não faz
  parte da identidade da pessoa; a mesma consulta pode atender ambos os papéis.
- `BankRecord` e `ReputationRecord` preservam cobertura e proveniência.
  Enums/contadores/booleans vêm dos tipos do motor; a fronteira valida sujeito,
  escopo, DV, timezone, cronologia, validade, provider e restrições aplicáveis.
  Datas são normalizadas em UTC. Dado recebido após expirar é válido como
  histórico **stale**, sem se tornar reputação limpa.

`FixtureBankProvider` e `FixtureReputationProvider` recebem datasets explícitos,
copiam coleções para tuplas e permanecem imutáveis. Ausência retorna
`not_covered`; indisponibilidade sintética é configurada explicitamente e lança
`ProviderUnavailable`; snapshot mais novo expirado retorna `stale`. Segurança
sem fixture tem snapshot `None`, nunca aparelho implicitamente registrado.
Não há endpoints HTTP artificiais nem chamadas externas nesses adapters.

Mudança de titularidade no mesmo `(tenant, branch, account)` é detectada na
carga das fixtures e na reconciliação com histórico local; exige reconciliação
explícita, sem misturar perfis. Não há workflow de onboarding nesta etapa.
Restrições com outro sujeito ou conta no snapshot bancário são erro de dados.

## Seleção temporal e material para o futuro cache

`selection.py` escolhe somente dados observados **e conhecidos** até `as_of`.
Ordena por `(observed_at, source_version)`, sem usar entrega tardia como prova
de atualidade. `source_version` é revisão dos dados, diferente da versão do
contrato do provider que integra a chave do cache.

A seleção compara resposta e snapshot anterior de mesmo escopo. O snapshot
mais novo conhecido continua sendo o watermark mesmo após expirar: resposta
limpa antiga não o substitui. Revisão nova pode encerrar relato/cancelar marcação;
mesma revisão com conteúdo/proveniência conflitante é `ProviderDataError`.
Duplicatas do mesmo fato mantêm o primeiro conhecimento; mudança de metadado
available→stale não é mudança do fato. Nenhum dado vencido vira limpeza.

`cache_updates` devolve fatos available/stale selecionados para a futura camada
de persistência. Não há cache global nem escrita em banco aqui. O resultado
bruto das consultas fica em `lookups`; `initial_result` conserva a cobertura
**selecionada na conclusão da checagem inicial** da origem. Sem retomada, é
`None` em COMPLETED. Esse registro é congelado na conclusão; uma retomada
preserva-o, inclusive unavailable, e revalida separadamente os fatos usados
no contexto atual. Não é um atestado de validade eterna.
Com watermark adverso vencido e resposta limpa antiga, `initial_result` é
stale/adverso; a resposta limpa permanece só em `lookups`. Se o lookup falhar,
`lookups` registra unavailable e a seleção ainda pode conservar watermark
stale. `security_lookup` conserva a resposta bancária bruta, distinta de
`security` selecionado. `received_at` registra o instante local de conclusão
de security/history e de cada consulta lógica DICT, inclusive falhas esgotadas;
não duplica recibo por retry nem por papel reaproveitado.
A PR 4 deve persistir esses materiais no protocolo de posse/fencing, sem tratar
indisponibilidade como sucesso limpo. Não descartar watermarks expirados ao
implementar o storage.

O TTL da política é usado nas coberturas sintéticas ausentes/indisponíveis;
fixtures com dados presentes declaram `valid_until` explicitamente. Não
estender ou renovar validade da fonte ao ler cache. Um adapter real deverá
construir a validade efetiva conforme fonte e orçamento acordados, sem atribuir
universalidade ao TTL de 300 s da demo.

## Montagem de contexto e DICT

`Enricher(bank, reputation, policy).enrich(...)`:

1. Recebe `SourceCheck.OWNED` ou `COMPLETED`, obrigatório. Estado em andamento
   ou não reivindicado não pode ser representado por um falso completed.
   Essa é uma pré-condição do chamador, **ainda sem implementação durável**.
2. Busca segurança atual e execuções bancárias por métodos separados e
   reconcilia os fatos. Ausência/indisponibilidade ficam explícitas.
3. Calcula `C_pre` com `preliminary` do próprio motor, incluindo segurança,
   histórico local e perfil bancário reconciliado, sem reputação DICT.
4. Usa evidência vigente de cache de escopo exato, mesmo quando C_pre é alto.
   Sem ela: remetente consulta apenas em OWNED; destinatário em C_pre <80.
   C_pre =80 pula destinatário; não pular a origem inicial só porque será rejeitada.
5. Coalesce leituras da mesma chave dentro dessa análise, inclusive falhas:
   uma consulta lógica, até um retry, não duas consultas independentes para
   a mesma pessoa. Cobertura desconhecida segue `queried`, nunca `cache` limpo.
6. Devolve Context imutável, security/history coverage, initial_result,
   lookups, cache_updates, security_lookup e received_at. Revalida cobertura
   no instante final. **Qualquer segurança bancária conhecida e stale** exige
   refresh, inclusive revogação/limite que venceram durante DICT ou histórico;
   não devolve Context que permita aprovação por ter perdido esses sinais.
   Cache de reputação usado que vence e C_pre final exigindo consulta ausente
   também lançam `EnrichmentNeedsRefresh`, sem devolver decisão.
   A exceção contém `partial: EnrichmentProgress`, com material normalizado
   para persistência e retomada. O coordenador futuro controla os ciclos e
   confirma posse/fencing antes de persistir. A biblioteca não grava análise,
   reserva, evento ou resultado inicial durável.

Troca de device, rejeição ou indisponibilidade não reinicia o gatilho da origem
quando o chamador fornece COMPLETED. Isso é testado como decisão da biblioteca,
sem alegar idempotência, sobrevivência a crash ou coordenação entre processos.
O chamador da PR 4/5 deverá comprovar posse, aguardar dono ativo, revalidar e
persistir conclusão inclusive indisponível, exatamente como guia §8.2.

`Context.provider_traces` alimenta a auditoria privada do DTO com provider,
versão, operação, permissão, proveniência e estados usados/consultados. Distingue
falha de lookup de evidência antiga ainda conhecida. Resposta pública continua
com três chaves de topo e sem prontuário/documentos de terceiros.

## Retomada no mesmo request

`EnrichmentNeedsRefresh.partial` é imutável e não contém Context/decisão.
Conserva `initial_result`, respostas DICT brutas (`lookups`), fatos selecionados
(`selected`/`cache_updates`), segurança selecionada/bruta e recibos acumulados.
O resultado bem-sucedido oferece o mesmo material em `Enrichment.progress`.
Somente os dados normalizados são material de persistência: o objeto de
continuação contém o deadline/callable local e **não** é um registro durável.

Retomar com `enrich(base_atualizado, ..., deadline=mesmo_deadline,
resume=error.partial)`. São conferidos Pix, tenant/conta/device/destino,
providers/versões, permissão, política e identidade do deadline. Material com
sujeito/tenant divergente falha antes dos providers. Não reutilizar entre
requests, nem criar outro deadline para ganhar 3 s extras. A checagem pode
continuar OWNED na mesma posse ou passar a COMPLETED após confirmação durável
pelo coordenador; completed não reabre a origem como owned.

Cada ciclo busca segurança/histórico atuais e reaplica watermarks anteriores.
O resultado inicial já concluído não é substituído nem dispara outra consulta
inicial. Respostas disponíveis vigentes são reaproveitadas; ausências/falhas
são memoizadas somente nesse request, inclusive mesma pessoa nos dois papéis,
sem se tornarem cache limpo ou sofrerem novos retries técnicos a cada ciclo.
Um destinatário cujo fato disponível expirou pode exigir leitura de refresh;
isso não reinicia a checagem inicial da origem. Fatos expirados não ganham TTL
novo. Recibos e respostas brutas acumulam sem reemitir recibo por reaproveitamento.

Os ciclos PostgreSQL/locks, limite de três revalidações e tratamento de crash
continuam responsabilidade da integração futura. Sem evidência estabilizada
ou orçamento restante, não converter refresh/BudgetExceeded em aprovação.
Relógio UTC regressivo continua erro explícito; o orçamento usa relógio
monotônico separado. Não foi adotado clamp de UTC que esconda o descompasso.

## Histórico bancário sem análises fictícias

`BankPayment` é um fato de execução confirmado: provider, operation_id, Pix
e ExecutionEvent `executed` correspondente, com ocorrência/conhecimento coerentes.
Não converter aprovado ou 2FA passado em pagamento; não criar HistoricalAnalysis
artificial, receipt, desafio ou cadastro de device para importar o histórico.

`BankLink(tenant, provider, operation_id, local_pix_id)` faz a ligação explícita
com uma operação local. O chamador fornece o conjunto completo de vínculos
verificados; uma associação omitida entre IDs diferentes não pode ser inferida
por valor/horário. A consulta/persistência de vínculos é responsabilidade da
PR 4, sem a qual não se declara reconciliação durável. Se IDs se sobrepõem
sem link, a biblioteca recusa a
ambiguidade. Operações de mesmo valor/instante/destino continuam distintas se
o ID bancário é distinto. Conflitos de fato/link, dois IDs bancários ligados à
mesma operação local do mesmo provider e ID de execução reutilizado são erros.
A conta, titulares, destino, valor e tempos financeiros devem concordar.
O próprio `Context` também recusa identidades bancárias duplicadas/conflitantes,
reutilização de ID de execução e sobreposição com IDs locais/da operação atual.
O repository futuro não pode contornar a reconciliação montando Context em dobro.
Identidades pertencem ao namespace tenant/provider; providers diferentes podem
ter o mesmo ID, sem inferir vínculo financeiro entre eles.

Execução vinculada é reaproveitada pela análise local, sem outro pagamento
importado. Uma execução já conhecida localmente pelo mesmo provider/ID (por
exemplo, ingerida pelo storage com `available_at` igual ao recebimento local) é
um único fato: a reconciliação não acrescenta uma segunda cópia. Execução sem análise local permanece em `Context.bank_payments`.
Ambas formam perfil somente quando conhecidas, na janela de 90 dias e maduras
pela **transação e execução** em t−24h. Perfis geral/específico e medianas usam
os pagamentos reconciliados; o fato sob análise nunca entra como anterior.

Execuções externas sem análise local **não contam como tentativas da nossa
API**, recebimentos por decision_at, fan-in de intenções, observação de device
ou exposição reservada pela API. Exposição das análises locais continua usando
seus eventos; limites financeiros externos devem vir do banco, incluindo
`bank_limit_exceeded`. Essa separação aplica o significado de tentativa do guia
§4.1 e evita promover seed do simulador a storage de produção. Não promete
visibilidade de todos os movimentos de uma instituição real.

## Prazo e cancelamento

`Deadline.after(3)` usa relógio monotônico. O chamador deve criar o orçamento
no início do futuro request e compartilhar a mesma instância entre operações.
`ProviderRunner` limita cada tentativa a min(0.25, restante), com no máximo
**um retry técnico**. `ConnectionError` (incluindo reset/refused),
`TimeoutError`, `socket.gaierror` e `ProviderUnavailable` são falhas técnicas.
`FileNotFoundError`, `PermissionError`, `ssl.SSLError` e demais OSError não
classificados propagam sem retry; problema de configuração não vira outage. Adapters devem traduzir exceções de
transporte específicas de seu SDK para `ProviderUnavailable`; dado inválido
lança `ProviderDataError`. `ValueError`/`RuntimeError` inesperados propagam sem
retry, sem esconder defeito de implementação como outage. Falha de dados não é retry; cancelamento do chamador se
propaga; orçamento total esgotado lança `BudgetExceeded`, sem resultado favorável
tardio. Timeout técnico esgotado, com orçamento total restante, vira cobertura
unavailable no enriquecimento.

O runner cancela chamadas async cooperativas e verifica o tempo antes/depois
de cada retorno. Adapters **não podem bloquear o event loop ou suprimir
cancelamento**. Não se promete interromper Python arbitrário, threads ou I/O
síncrono. O `RestConnector` original aceita `.25` e recusa zero/negativo/NaN/Inf;
isso continua sendo timeout de socket, não deadline total. Um futuro adapter
remoto precisa respeitar o contrato async/cancelamento; chamar requests
síncrono diretamente no event loop não atende ao contrato. Nenhuma integração
remota é declarada pronta nesta PR. Banco síncrono continuará no threadpool
na integração HTTP futura; esta etapa não altera os resources existentes.

## Exemplo executável da biblioteca

Com dependências de dev instaladas, na raiz do Git:

```bash
PYTHONPATH=src .venv/bin/python - <<'PY'
import asyncio
from datetime import datetime, timezone
from antifraud.context import Pix, AccountKey, DestinationKey, Context
from antifraud.policy import Policy
from antifraud.engine import evaluate
from providers.contracts import ProviderIdentity
from providers.fixtures import FixtureBankProvider, FixtureReputationProvider
from providers.enrichment import Enricher, SourceCheck
from providers.budget import Deadline

async def demo():
    now = datetime(2026, 10, 17, 17, tzinfo=timezone.utc)
    pix = Pix('demo', AccountKey('demo', '0001', '88213', '12345678909'),
              '98765432100', DestinationKey('60701190', '3675', '10442'),
              5000, now, datetime(2020, 1, 1, tzinfo=timezone.utc), 'observed-device')
    identity = ProviderIdentity('synthetic-demo', 'v1')
    outcome = await Enricher(FixtureBankProvider(identity), FixtureReputationProvider(identity), Policy(), clock=lambda: now).enrich(
        Context(pix, now), source_check=SourceCheck.OWNED, deadline=Deadline.after(3))
    decision = evaluate(outcome.context)
    print(decision.status, decision.score, outcome.security.state,
          dict(outcome.context.dict_actions))
asyncio.run(demo())
PY
```

Saída: `approved 10 not_covered {'payer': 'queried', 'payee': 'queried'}`.
É uma simulação explícita da biblioteca com R$50 e dados ausentes, não uma
análise persistida/HTTP ou prova de posse concorrente. O score vem das regras;
o adapter não retorna aprovação fixa.

## Validação de device na fronteira

Device contendo apenas whitespace é erro de entrada 400 `QIT000001`, path
`source.device_id`, antes de providers/reserva. O motor puro também o recusa.
Device não vazio mantém todos os bytes/caracteres, inclusive espaços nas pontas;
não normalizar a identidade por strip. É uma restrição local adotada para
alinhar contrato e `SecurityScope`, não uma exigência atribuída ao PDF. O schema
oficial e as fontes históricas foram preservados, com a validação semântica
no normalizador de runtime. Não há mudança nos três status ou no dinheiro.
