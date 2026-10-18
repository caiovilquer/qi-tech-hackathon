# Guia de implementação — Antifraude Pix

Versão 3, 17/10/2026. Política inicial: `pix-experimental-v3`.

**Este é o guia vigente para escrever o código.** Substitui as escolhas incompatíveis da avaliação v2 e as alternativas ainda abertas nos documentos de pesquisa. O enunciado continua sendo a fonte dos requisitos oficiais. Os limiares abaixo são escolhas experimentais nossas, fixadas para tornar a implementação reproduzível; não são valores calibrados em fraudes reais nem uma certificação de conformidade com o Banco Central.

**O que já está entregue:** contrato e normalização em Python, motor puro de referência, simulador sequencial, fixtures e testes. **O que ainda deve ser implementado:** endpoints na Base API, persistência PostgreSQL, integração dos provedores, transações e testes HTTP/concorrentes. Não há API bancária pronta ou integração real com o DICT nesta entrega.

**Revisão concluída:** um agente independente, sem o histórico da discussão, revisou os arquivos e conferiu novamente as correções. Os **92 testes passaram**, sem achado P1/P2 pendente no guia e na referência. Escopo, achados e evidências estão em [REVISAO-INDEPENDENTE-GUIA.md](REVISAO-INDEPENDENTE-GUIA.md).

## 1. Mudanças que precisam ser lidas primeiro

1. `approved`, sucesso de 2FA e Pix executado são três fatos diferentes. Somente execução confirmada pelo banco alimenta o perfil de pagamentos. A análise original permanece imutável.
2. Dois Pix com IDs diferentes contam como duas tentativas, mesmo que tenham valor, destino e horário iguais. Repetir o mesmo ID e conteúdo devolve a resposta gravada, sem pontuar ou consultar novamente.
3. Aparelho observado não se torna aparelho autorizado. Tentativas recusadas e desafios falhos não criam confiança.
4. Mantemos a decisão do grupo: DICT do remetente na primeira solicitação válida e distinta observada por **conta**; do destinatário conforme `C_pre`. O complemento do Claude não muda esse acordo.
5. Mock sem fixture retorna `not_covered`, nunca “zero fraudes confirmado”. Indisponibilidade e falta de cobertura ficam explícitas.
6. O perfil usa 90 dias, execução confirmada e maturidade de 24 horas. Duas execuções maduras para a mesma conta destinatária já permitem uma referência específica, independentemente do perfil geral.
7. Há dois patamares noturnos próprios: desafio acima de R$ 1.000 e rejeição acima de R$ 3.000 nas condições da seção 6. Não representam autorização para ultrapassar um limite operacional do banco.
8. A validação cobre DV de CPF/CNPJ, CNPJ alfanumérico, datas reais, números finitos e hash de todos os campos. Dinheiro é sempre inteiro em centavos.
9. As consultas externas ficam fora do motor e fora dos locks PostgreSQL. O contexto é relido sob lock antes da decisão final.
10. O guia diferencia os testes já executáveis daqueles que só poderão passar depois de implementar a API e o banco.

## 2. Requisitos, fronteira e arquitetura

O [enunciado](<Hackathon Antifraude Pix.pdf>), páginas 19–25, pede `POST /pix/transaction`, `GET /pix/transaction/{id}`, três saídas (`approved`, `rejected`, `two_factor_required`), motivos, histórico próprio, Base Service, testes e README. O topo da resposta tem exatamente `key`, `status` e `metadata`. Onboarding, frontend, execução de Pix e implementação do desafio 2FA ficam fora do desafio. Dados externos simulados foram autorizados pela organização, conforme relato do grupo; isso não altera o payload.

Usar um único serviço Python inicialmente. Separar **responsabilidades e interfaces** é necessário; separar em vários processos HTTP não é. O motor recebe dados prontos e é agnóstico ao PostgreSQL, ao banco parceiro e à origem real ou simulada dos dados.

```text
Resource + validação → Controller / orquestrador
                         ├─ Repository → PostgreSQL
                         ├─ BankContextProvider → fixture ou banco parceiro
                         ├─ ReputationProvider → fixture ou adaptador DICT
                         └─ Context imutável → regras → Decision → DTO
```

O controller coordena a operação e o commit; o repository lê e grava; os providers enriquecem; o motor avalia. O motor não importa SQLAlchemy, cliente HTTP, Redis ou relógio global. `decision_at` entra no contexto. Providers reais e mocks implementam os mesmos contratos; `provenance` identifica a fonte para auditoria, sem criar regras do tipo `if mock`.

### 2.1 Base efetivamente fornecida

Referência: [bootcamp-base-api.zip](bootcamp-base-api.zip), SHA-256 `764a8bd991df9bd1a1e879733a17af886682abbb0d9ab97384de8b08adfc7216`.

A base usa FastAPI, JSON Schema, SQLAlchemy 2, psycopg2 síncrono e PostgreSQL. Preservar Resource → Controller → Repository/Model → DTO, contexto de requisição, autenticação por `INTERNAL-TOKEN`, logger e session manager. As sessões têm `autoflush=False`; executar `flush()` onde for necessário materializar chave/constraint. Repositories não dão commits independentes na transação do Pix. O webhook de exemplo da base não é um subsistema de 2FA.

Os providers encapsulam os connectors da base. Para o orçamento de 250ms, ajustar o contrato/configuração de timeout do connector para segundos fracionários positivos (`0.25`), sem conversão para `int`/zero, e aplicar o deadline total no orquestrador. O timeout de socket de uma biblioteca HTTP não é sozinho um prazo total garantido. Fixtures locais são chamadas em memória; não precisam de serviços HTTP artificiais.

Estrutura a adaptar às pastas existentes, sem substituir o framework da base:

```text
src/resources/pix_transaction.py       # POST e GET
src/schemas/post_pix_transaction.json  # schema entregue nesta revisão
src/controllers/pix_transaction.py     # pipeline, idempotência e transação
src/repositories/                     # Pix, análise, eventos e consulta inicial
src/models/                           # entidades SQLAlchemy
src/dtos/pix_transaction.py            # resposta pública congelada
src/antifraud/context.py               # tipos imutáveis
src/antifraud/engine.py                # avaliação e precedência
src/antifraud/rules/                   # critérios registrados
src/antifraud/policy.py                # parâmetros versionados
src/providers/                        # contratos + fixtures + adaptadores
```

O arquivo de referência mantém o nome [politica_v2_simulacao.py](politica_v2_simulacao.py) para facilitar a comparação, mas seu conteúdo atual é V3. Separar seus componentes ao portá-lo para a API; não importar o simulador como repository de produção.

### 2.2 Responsabilidade do banco chamador

O banco autentica o cliente, valida permissão sobre a conta, saldo ou crédito, limites operacionais e integridade dos dados enviados. `channel=app` não prova dispositivo registrado, liveness recente, posse do aparelho ou inexistência de fraude. Análise de crédito pertence ao motor de crédito, inclusive em Pix no crédito.

Nosso motor avalia risco do pagamento e da contraparte. Uma restrição aplicável ao destinatário pode justificar rejeição antes da execução; um alerta de interface anterior à solicitação não substitui essa avaliação. Contagem adversa isolada gera sinal de risco, sem inventar uma proibição oficial. Aprovação antifraude não obriga o banco a pagar nem supera seus controles.

## 3. Contrato público e normalização

### 3.1 Entrada oficial

```json
{
  "id": "pix-000123",
  "amount": 150000,
  "transaction_date": "2026-10-17T22:15:00-03:00",
  "source_account": {
    "branch": "0001", "account_number": "88213",
    "opening_date": "2025-03-02T10:00:00-03:00",
    "owner": {"document_number": "123.456.789-09"}
  },
  "destination_account": {
    "participant": "60701190", "branch": "3675", "account_number": "10442",
    "opening_date": "2026-10-10T09:00:00-03:00",
    "owner": {"document_number": "987.654.321-00"}
  },
  "source": {
    "device_id": "dvc-9f2c71a8", "channel": "app", "ip": "198.51.100.23",
    "geolocation": {"latitude": -23.56, "longitude": -46.65}
  }
}
```

Não acrescentar renda, saldo, cadastro do aparelho, chave Pix ou resultado de 2FA ao payload exigido. Eles pertencem ao contexto enriquecido quando disponíveis. `amount=150000` significa R$ 1.500,00.

### 3.2 Regras da fronteira

Implementação de referência: [contrato_pix.py](contrato_pix.py) e [post_pix_transaction.json](post_pix_transaction.json). O schema usa Draft 2020-12 sem referências internas, para funcionar com o `RefResolver` da base recebida. O schema sozinho não substitui validações semânticas.

| Campo | Escolha de implementação |
|---|---|
| `id` | String imprimível, 1–128 caracteres; recusar `/`, `?`, `#`, controles e os IDs exatos `.` e `..`. Preservar o texto. Codificar o segmento de URL ao consultar. |
| `amount` | Inteiro matemático entre 1 e 100.000.000.000 centavos. `150000.0` integral é aceito e vira inteiro; booleano, fração, NaN e infinito são recusados. Nunca arredondar. |
| Agência / conta | Strings de 1–4 / 1–20 dígitos ASCII, respectivamente. Preservar zeros à esquerda. |
| `participant` | Oito caracteres ASCII alfanuméricos; normalizar letras em maiúsculas. Não é prova de participação real no Pix. |
| Documento | CPF numérico de 11 posições ou CNPJ de 14, com primeiras 12 posições alfanuméricas e dois DVs numéricos. Aceitar sem máscara ou máscara completa; remover máscara e normalizar letras. Validar DV; isso não comprova existência nem titularidade. |
| Datas | RFC 3339 com timezone, até seis casas decimais, calendário e offset válidos. Normalizar UTC. Ano UTC da transação entre 0002 e 9998, limite técnico para aritmética de janelas/timezones sem overflow; aberturas aceitam os anos representáveis 0001–9999 se anteriores à transação. Não rejeitar o exemplo do PDF apenas por não ser de hoje. |
| Canal | String não vazia após `strip`, normalizada em minúsculas; não inferir autenticação a partir do canal. |
| Dispositivo | Identificador textual obrigatório, sem transformação de caixa. Não representa um cadastro autorizado por si só. |
| IP | String obrigatória. IP inválido produz `null` no contexto e `IP_INVALID` em qualidade; não pontua sozinho. Preservar seu texto original no hash para detectar alteração. IPv6 normalizado; não usar truncamento `/64` como identidade. |
| Geolocalização | Opcional ou `null`; ambas as formas têm o mesmo significado. Coordenadas finitas nos intervalos [-90,90] e [-180,180]. `(0,0)` é válido. Não arredondar. |
| Estrutura | Recusar campos adicionais, tipos inesperados, NUL e surrogates Unicode isolados em qualquer string/chave, inclusive escapados em JSON; esses caracteres não são persistíveis em JSONB. Unicode válido continua permitido. Limite de corpo HTTP escolhido: 64 KiB, tratado antes de decodificar. |

Esses comprimentos e limites são restrições locais de implementação, não exigências atribuídas ao PDF. Para dinheiro, o parsing HTTP deve preservar o valor decimal antes de qualquer conversão para `float`; `normalize_payload` não recupera precisão perdida por um parser anterior. Na integração HTTP, usar `normalize_json(raw_body)` ou `parse_payload_json(raw_body)` seguido de `normalize_payload`. Esses helpers recusam chaves JSON duplicadas, constantes não JSON e coordenadas cuja precisão decimal não sobreviva à conversão para `float` e volta por `str(float)`, inclusive underflow; retornam 400, sem arredondar silenciosamente.

**Adaptação necessária na base:** o padrão `on_post(payload: dict)` já deixa o FastAPI decodificar números antes da nossa função. Para esta rota, receber `Request`, ler os bytes com limite de tamanho e chamar o parser estrito antes da validação/normalização. Não usar `request.json()` ou a conversão automática para `dict` como primeiro parser. Manter o Resource fino; encaminhar o processamento síncrono do controller ao mecanismo de execução apropriado da base, sem bloquear o event loop.

### 3.3 Idempotência e retorno

Chave de idempotência: `(tenant_id, id)`. O tenant é resolvido pela credencial do banco, nunca por campo novo enviado pelo cliente. Cada tenant representa uma instituição de origem; seu ISPB, ausente no payload, é configuração do servidor.

Gerar SHA-256 do envelope canônico definido pelo normalizador, cobrindo **todos** os campos oficiais, inclusive IP, canal, localização e abertura da origem. Datas equivalentes em UTC, documentos com/sem máscara, valor integral e geolocalização ausente/`null` normalizam de modo consistente. Não inventar deduplicação por valor, CPF, destino ou janela de tempo.

| Situação | Resposta HTTP escolhida |
|---|---|
| Nova solicitação válida, qualquer uma das três decisões | 201 com análise persistida |
| Mesmo tenant/ID e mesmo hash | 200 com o mesmo `key`, `status` e `metadata` gravados |
| Mesmo tenant/ID e conteúdo diferente, inclusive ID reservado em processamento | 409 `QIT002002` |
| GET de análise existente | 200; busca pelo `id` externo, não pelo `key` |
| GET inexistente naquele tenant | 404 `QIT002001` |
| JSON/schema/tipo/formato inválido | 400 `QIT000001`, conforme envelope da base |
| DV inválido | 422 `QIT002003` |
| Data/horário/offset semanticamente inválido | 422 `QIT002004` |
| Abertura posterior à transação | 422 `QIT002005` |
| Corpo excede limite HTTP | 413 `QIT002009`, no padrão da base |
| Falha técnica que impede persistência/resultado confiável | 503 `QIT002008`; repetir mesmo ID e corpo |

Preservar os erros de autenticação existentes na base. Antes de adicionar classes de erro, executar a verificação de códigos duplicados; não reutilizar os códigos `QIT001001`–`QIT001007` já existentes.

Exemplo de formato de resposta; valores ilustrativos, não o resultado prometido para qualquer fixture:

```json
{
  "key": "58ce6c65-0d45-4d5f-b5c8-2e65585a3fb8",
  "status": "two_factor_required",
  "metadata": {
    "policy_version": "pix-experimental-v3",
    "score": 35,
    "confidence_pre": 35,
    "reasons": ["NIGHT_SOFT_LIMIT", "LOW_HISTORY_EXPOSURE", "RECENT_PAYEE_ACCOUNT", "DICT_CHECK_UNAVAILABLE"],
    "decision_at": "2026-10-18T01:15:00Z",
    "data_quality": [],
    "enrichment": {
      "payer": {"action": "queried", "state": "not_covered"},
      "payee": {"action": "queried", "state": "not_covered"}
    }
  }
}
```

Guardar também a contribuição por família, pisos de decisão e snapshot auditável. O DTO público pode resumir o snapshot; não devolver prontuário de fraude, documentos de terceiros ou resposta bruta do bureau em `metadata`. Não incluir novas chaves no topo. Replay e GET devolvem o DTO congelado, mesmo depois de 2FA, execução ou troca da política.

## 4. Dados internos e significado do histórico

### 4.1 Identidades e fatos

| Conceito | Identidade / significado |
|---|---|
| Conta pagadora | `(tenant, branch, account_number, document_number)` normalizados; equivale a `AccountKey`. A integração deve detectar alterações de titularidade na mesma conta, não somar perfis silenciosamente. |
| Conta destinatária | `(participant, branch, account_number)`, `DestinationKey`. CPF/CNPJ identifica a pessoa; não substitui a conta. |
| Tentativa | Solicitação válida e distinta analisada, inclusive rejeitada ou com 2FA. Replay não cria tentativa. |
| Execução | Fato bancário explícito `executed`, ligado à operação. Ausência de confirmação é desconhecida, não pagamento. |
| Exposição | Valor que pode ter sido ou ainda pode ser executado: aprovado, desafio pendente ou executado. Rejeição não reserva exposição. Falha/cancelamento financeiro confirmado ou falha terminal do desafio correto libera exposição. |
| Perfil | Somente execuções confirmadas e maduras; não inclui “aprovados presumidos”, rejeitados ou 2FA aprovado sem pagamento. |
| Fan-in | Tentativas dirigidas à mesma conta por contas pagadoras distintas, na visibilidade da nossa API. Não é afirmação de que a conta recebeu esses valores. |

O simulador usa `AccountKey` para contar pagadores distintos. Um titular com duas contas pode ser contado duas vezes: isso é diversidade de contas, não de pessoas. Se for acrescentada diversidade por CPF/CNPJ, criar outra feature explícita e testar separadamente.

Histórico, fan-in e cache são isolados por tenant. O mesmo destino em outro cliente da API não contribui automaticamente para a análise atual. Inteligência compartilhada entre instituições exigiria uma fonte e um contrato próprios; não inferi-la de registros privados de outros tenants.

Histórico importado do banco deve ter identificador estável da operação, conta, valor, instante e execução verificável. Reconciliar com o Pix local pelo identificador bancário, nunca por semelhança de valor/horário. Não contar duas vezes uma operação que apareceu no histórico e na própria API.

### 4.2 Tempo

Usar `TIMESTAMPTZ` e relógio UTC no servidor. Janelas comportamentais usam o `transaction_date` emitido pelo backend bancário confiável; noite e dias civis usam `America/Sao_Paulo`. Consultas e validade de segurança usam `decision_at`, instante capturado pelo servidor para a análise final.

Para uma transação em `t`, histórico comportamental pertence a `(t−90 dias, t]` e só pode conter fatos disponíveis até `decision_at`. Transações distintas no mesmo instante entram segundo a ordem já persistida; excluir somente a operação atual. Rajadas e total de tentativas usam todos os status. Histórico maduro exige tanto a transação quanto a execução em ou antes de `t−24 horas`.

Há também uma guarda de frequência por `decision_at` em dez minutos, independente da data declarada. Ela impede escapar da rajada apenas espaçando timestamps do payload. A contribuição de frequência é o máximo entre os dois relógios, sem somar 30+30. Isso não torna a API resistente a toda data arbitrariamente adulterada: o contrato de integração exige que o banco emita timestamps confiáveis; controles de atraso específicos de produção precisam ser acordados sem invalidar fixtures do desafio.

**Evento atrasado:** pagamento executado às 10h, conhecido pelo antifraude às 10h05. Uma análise das 10h03 não pode usar o evento. Uma análise posterior pode usá-lo, respeitando maturidade e janela. Não reescrever decisões passadas. Persistir `occurred_at` e `received_at`/`available_at`, além do identificador e da ordem do evento na origem.

Segurança atual não é consultada “no passado” usando a data do Pix. Revogação de aparelho e restrição vigente são verificadas em `decision_at`. O `asof` de fixtures é recurso de simulação temporal; não é uma operação histórica oferecida pelo DICT real.

## 5. Enriquecimento, DICT e confiança preliminar

### 5.1 Contratos de providers

`BankContextProvider` entrega, no escopo exato de conta/dispositivo/destino: registro ou revogação do aparelho, recuperação recente, restrições aplicáveis, eventual designação bancária explícita de beneficiário confiável e violação de limite operacional. Histórico e eventos de execução/2FA podem ser obtidos pelo mesmo adapter, com métodos separados. O motor não precisa saber se essas informações vieram de uma ou várias bases.

`ReputationProvider` entrega evidência normalizada por CPF/CNPJ, com `state`, contadores agregados disponíveis, restrições explícitas quando realmente fornecidas, escopo, `observed_at`, `available_at`, `valid_until` e `provenance`. Estados: `available`, `not_covered`, `unavailable`, `stale`. Só `available` vigente é evidência utilizável. Não transformar timeout, sujeito sem fixture ou dado vencido em ausência de fraude.

Na entrada dos providers, validar enums, escopo/sujeito, contadores não negativos, timestamps com timezone e validade coerente. Identificar revisão do snapshot por `(observed_at, source_version)`; `source_version` é inteiro não negativo, zero quando a fixture não tem revisões simultâneas. `available_at` apenas determina se o dado já era conhecido. Um snapshot antigo recebido depois não pode substituir uma revogação/reputação observada mais recentemente. Quando o cache mais recente expirar e só chegar dado anterior, usar `stale`, sem recuperar um atestado antigo de limpeza. Duas versões de mesmo identificador com conteúdo conflitante são erro do provider, não escolha arbitrária de confiança.

Estatísticas DICT sobre a pessoa não provam que uma conta específica é fraudulenta. Não há renda, salário, saldo, valor de ticket médio, confiança de dispositivo ou resultado de 2FA no DICT. A consulta por pessoa é pertinente ao payload; operações por chave Pix exigem uma chave que não está na entrada. Contagens agregadas e marcações não equivalem automaticamente a uma restrição ativa. A listagem de marcações tem limites de acesso/escopo; não presumir acesso irrestrito a todos os registros do ecossistema.

Nas fixtures V3 os contadores são fornecidos explicitamente, sem reconstruir incorretamente as janelas do DICT. Se implementar esse cálculo depois, respeitar a definição de cada campo: por exemplo, a janela `m12` de estatísticas relevantes considera meses completos anteriores, não “365 dias incluindo o mês atual”. Cancelamento de marcação e encerramento de relato devem alterar o snapshot seguinte. Ver [API oficial do DICT](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/API-DICT.html).

### 5.2 Coeficiente de seleção de consulta

Calcular primeiro todas as regras locais, incluindo o snapshot de segurança bancária, e excluir somente os sinais DICT dessa soma:

```text
local_score = soma das contribuições locais já limitadas por família
uncertainty = min(40, 20 × quantidade de condições abaixo verdadeiras)
  - perfil geral insuficiente
  - cadastro do dispositivo desconhecido
  - destinatário sem execução madura anterior para esta conta
C_pre = max(0, 100 − min(100, local_score + uncertainty))
consultar destinatário quando C_pre < 80
```

`C_pre` é um índice ordinal de seleção, não probabilidade de legitimidade. Um aparelho registrado sozinho não transforma `C_pre` em 100. Confiança não apaga restrições já conhecidas. Usar a mesma regra pura no cálculo preliminar e na avaliação final, para evitar dois motores divergentes.

### 5.3 Quando consultar

1. Resolver replay/conflito antes de providers ou novos contadores.
2. Buscar contexto bancário atual para cada ID novo e aproveitar evidência de reputação vigente já disponível.
3. Remetente: consultar na primeira solicitação válida e distinta **observada por conta nesta API**, inclusive se terminar rejeitada ou com 2FA. Não significa primeiro Pix liquidado da vida da conta. Mudança de aparelho não reinicia essa condição. Cadastro novo sem histórico segue a política de pouco histórico.
4. Destinatário: consultar se `C_pre < 80` e não houver evidência vigente adequada. Se `C_pre ≥ 80`, registrar `skipped_confidence`; usar restrição ou evidência adversa vigente já conhecida mesmo assim.
5. Na conta remetente já inicializada, não consultar de novo somente porque chegou outro Pix; registrar `skipped_after_initial` quando não houver cache vigente. A consulta inicial antiga não comprova situação atual limpa. Novas restrições bancárias vêm do provider de segurança atual, independentemente desse gatilho.

TTL de referência: 300 segundos, uma escolha da demo. Em adaptação real respeitar validade/frescor da operação e fonte, sem afirmar que esse é o TTL universal do DICT. Cache de produção deve incluir tenant, documento, operação, escopo e versão do provider. O cache em memória do simulador usa `(tenant, documento)`; suas fixtures descrevem um universo sintético comum por documento, não uma base privada real de clientes. Em produção, o adapter deve aplicar as permissões do tenant também na origem.

`not_covered`, `unavailable` ou `stale` de uma consulta necessária geram `DICT_CHECK_UNAVAILABLE`, +10 uma única vez na família Contraparte. Isso não implica desafio automático: R$ 50 sem outros sinais pode resultar aprovado com score 10. Consulta não solicitada não equivale a falha e não recebe esse ponto. O snapshot distingue todos os casos.

O registro inicial do remetente é durável e possui os estados `unclaimed → in_progress → completed`. `completed` guarda inclusive uma resposta indisponível. Um crash durante `in_progress` permite retomada com lease e fencing; é conclusão da checagem inicial, não uma nova política de consulta por transação. Não prometer exatamente uma chamada remota em caso de crash: uma leitura externa pode ser repetida, mas apenas um resultado inicial é aceito para aquele token de posse.

## 6. Política numérica inicial

Todos os valores desta seção são **política própria de demonstração**. Estão fixados para implementação e testes, mas devem ser apresentados como parâmetros experimentais. Não calcular “taxa de acerto” a partir das próprias fixtures.

Famílias e tetos: Limite 30; Velocity 30; Montante 30; Perfil 30; Acesso 40; Contraparte 50. Somar sinais dentro da família até o teto, depois somar famílias. Score <30 aprova; 30–69 pede 2FA; ≥70 rejeita. Piso explícito de rejeição prevalece sobre tudo; piso de 2FA prevalece sobre aprovação por score. Score não é percentual e pode ultrapassar 100. Não há bônus negativo para anular reputação adversa.

### 6.1 Perfil, frequência, valor e destino

| Regra / código | Condição exata | Contribuição |
|---|---|---|
| Perfil geral | ≥5 pagamentos executados e maduros, distribuídos em ≥3 dias civis, dentro de 90 dias | Habilita referência geral; não pontua |
| Perfil específico | ≥2 pagamentos executados e maduros para a mesma `DestinationKey` | Habilita referência específica, mesmo sem perfil geral |
| `LOW_HISTORY_EXPOSURE` | Sem ambos os perfis: PF valor >R$1.000 / >R$5.000; para PJ multiplicar cortes por 5 | Limite +15 / +30, usar somente o maior |
| `AMOUNT_ABOVE_PROFILE` | Valor >2× / >5× mediana da conta destinatária quando perfil específico existir; caso contrário mediana geral | Perfil +15 / +30, somente o maior |
| `NEW_PAYEE` | Há perfil geral, nenhuma execução anterior para a conta destinatária na janela, titular diferente | Contraparte +10 |
| `VELOCITY_BURST` | Tentativas em `(t−10min,t]`, incluindo atual: PF ≥5 / ≥10; PJ ≥25 / ≥100 | Velocity +30 / piso rejeição |
| `RECEIPT_VELOCITY` | Mesmos cortes, olhando `decision_at` em dez minutos | Mesmo resultado; contribuição conjunta com regra anterior limitada a 30 |
| `DAILY_AMOUNT` | Pelo menos duas tentativas nas últimas 24h; soma >3× mediana das somas diárias de execuções maduras se há perfil geral; sem perfil, >R$5.000 PF ou >R$25.000 PJ | Montante +20 |
| `STRUCTURING` | ≥3 tentativas em 24h na faixa inclusiva de 85%–100% de R$1.000, R$3.000 ou R$5.000; atual também na mesma faixa | Montante +30, uma vez |
| `RECENT_PAYEE_ACCOUNT` | Idade da conta destinatária <7 dias / de 7 a <30 dias | Contraparte +25 / +10 |
| `SELF_TRANSFER_NEW_ACCOUNT` | Mesmo CPF/CNPJ, conta destinatária <7 dias | Contraparte +15 adicional; sem dividir score ao meio |
| `MULE_FAN_IN` | Conta destinatária <30 dias; ≥5 contas pagadoras distintas em 24h; e valor atual ≥R$500 **ou** soma de tentativas ≥R$5.000 | Contraparte +40 se destino PF; +20 se PJ |

`DAILY_AMOUNT` usa somente dias com execuções maduras na mediana; não preenche dias inativos com zero. Tentativas rejeitadas continuam nos totais de frequência/montante, para detectar repetição. Não confundir isso com exposição financeira. Mesmo valor e mesma conta em IDs distintos nunca são descartados. As comparações são em centavos/razões exatas, sem arredondar medianas monetárias.

Ser PF, PJ ou MEI não dá imunidade. Um vendedor com CPF pode ter muitos pagadores legítimos; `MULE_FAN_IN` é combinação de idade, diversidade e montante, não uma lista negra por volume. Não implementar “muitos dispositivos: rejeita PF e só desafia PJ”. Identificar PF/PJ pelo documento não revela sozinho que uma pessoa jurídica é MEI ou que existe exceção regulatória aplicável.

### 6.2 Acesso e reputação

| Regra / código | Condição | Resultado |
|---|---|---|
| `DEVICE_REVOKED` | Provider confirma aparelho revogado e dado vigente | Rejeitar |
| `UNREGISTERED_DEVICE` | Provider confirma aparelho não registrado | Acesso +20 |
| `DEVICE_CHANGE` | Estado desconhecido e há outro dispositivo observado no histórico comportamental | Acesso +20; tentar outra vez não elimina o sinal |
| `DEVICE_POLICY_LIMIT` | Nas duas situações anteriores, valor >R$200; para explicitamente não registrado, também soma de exposição desse grupo no dia >R$1.000 | Piso 2FA, política própria |
| `RECENT_ACCOUNT_RECOVERY` | Recuperação de conta conhecida e ocorrida nas últimas 24h de `decision_at` | Acesso +25 |
| `PAYER/PAYEE_BANK_RESTRICTED` | Restrição ativa e aplicável, com escopo e vigência explícitos | Rejeitar |
| `BANK_EFFECTIVE_LIMIT` | Banco informa violação de limite operacional vigente | Rejeitar; 2FA não supera isso |
| `PAYER/PAYEE_DICT_ADVERSE` | Evidência vigente do sujeito, fraude agregada ≥1 **ou** relato aberto ≥1 | Contraparte +30 por documento distinto, com teto 50 da família; mesma pessoa nos dois papéis não duplica o sinal |
| `PAYER/PAYEE_DICT_RESTRICTED` | Fonte fornece restrição explícita vigente e aplicável; não inferida da contagem | Rejeitar |
| `DICT_CHECK_UNAVAILABLE` | Consulta necessária sem evidência utilizável | Contraparte +10 total, mesmo se ambos os papéis falharem |

Estado `unknown` sem histórico de outro aparelho não ganha automaticamente os pontos de troca de dispositivo, mas reduz `C_pre`. Cadastro bancário válido de um S24 torna seu estado `registered`; não é necessário “aprender” esse aparelho por várias tentativas. A troca não remove sinais de valor, destino ou reputação.

### 6.3 Noite, agregação e limites

Noite: [20h, 06h), horário de São Paulo. Às 02h, o início da janela é 20h do dia anterior. Somar exposição ainda possível, inclusive análise com 2FA pendente; não somar rejeições. Não reiniciar na meia-noite.

* `NIGHT_SOFT_LIMIT`: origem PF, destino PF de outro titular, exposição da noite para PF de outro titular somada ao atual **>R$1.000** → piso 2FA.
* `NIGHT_HARD_LIMIT`: origem PF; destino PF de outro titular sem designação bancária explícita de beneficiário confiável **ou** conta do mesmo titular aberta há menos de sete dias; exposição das operações qualificadas da noite somada ao atual **>R$3.000** → rejeitar.
* Guardar em cada análise se ela se qualificava para o patamar rígido naquele instante. Não reclassificar silenciosamente tentativas antigas após mudança da lista de confiança.
* Uma conta paga duas vezes pode ter referência de valor, mas não vira beneficiário cadastrado/confiável do banco. Essa designação vem do provider e tem escopo e validade.
* Sucesso de 2FA sozinho não libera exposição nem suspende o patamar rígido. Limite operacional bancário informado como excedido sempre rejeita, mesmo que o score fosse baixo.

Esses critérios procuram demonstrar acumulação e risco de coação; não afirmam que 2FA resolve coação. Os cortes estritos `>` significam que exatamente R$1.000/R$3.000 não cruzam o respectivo patamar, embora outras regras possam atuar.

## 7. 2FA, execução e estados de suspeita

### 7.1 Três trilhas separadas

```text
Análise: inexistente → approved | two_factor_required | rejected  (imutável)
Desafio: pending → passed | failed_terminal | expired
         pending → failed_nonterminal → pending/pass/failed_terminal
Execução: unknown/pending → executed | failed | cancelled
```

O banco recebe `two_factor_required`, cria e executa o desafio e decide se pode continuar **a mesma operação**, observando os demais bloqueios. O vínculo inclui tenant, ID do Pix, `key` da análise, challenge ID e fingerprint da operação (origem, destino, valor). Uma aprovação de desafio não autoriza outro valor/destino nem outro Pix. `challenge_id` é identificado pelo banco; o ID sintético do simulador só serve às fixtures.

O banco pode executar o Pix após desafio aprovado sem chamar novamente o POST. Nosso provider recebe/consulta o fato de autenticação e, separadamente, o resultado financeiro. Se o POST original for repetido, responde com a análise original de 2FA; isso é replay correto. GET também não vira `approved` porque o pagamento foi feito.

Para a próxima operação, o provider pode fornecer cadastro atualizado do aparelho ou uma autorização contextual explícita emitida pelo banco. **O evento “2FA passou” sozinho não registra aparelho nem cria confiança global.** Uma autorização de viagem, se implementada, precisa de conta, dispositivo, contexto/localidade, validade e revogação. As regras geográficas não estão ativas na V3, portanto não existe hoje um desafio geográfico que a segunda transação precise “desligar”. Ao adicioná-las, testar cenário sem autorização, autorização vigente e autorização expirada, sem dispensar outras regras.

O MVP não precisa de novo endpoint de webhook. Providers/fixtures podem carregar eventos autenticados e o serviço persiste os fatos no histórico interno. Caso se acrescente webhook interno, ele deve usar autenticação própria, idempotência e verificação de vínculo, não ser exposto como terceira API exigida pela banca.

### 7.2 Ingestão de eventos

Identificar evento por `(tenant, provider, event_id)`. Mesmo ID e mesmo payload é replay; mesmo ID e conteúdo diferente é conflito `QIT002006`, sem alterar fato aceito. Evento inválido ou incompatível com estado terminal gera `QIT002007`. Nunca aceitar `failed/cancelled` depois de `executed` como forma de apagar um pagamento: estorno/devolução é outro fato financeiro, fora deste protótipo.

Usar ordem/versionamento da fonte, horário de ocorrência e horário de conhecimento. Reordenamento de entrega não pode ressuscitar um desafio encerrado. Falha não terminal pode preceder sucesso; falha terminal/expiração não pode virar sucesso no mesmo desafio. Para liberar exposição, exigir falha terminal vinculada ao desafio exato daquela análise. O payload público não pode fabricar esses eventos.

Eventos de segurança possuem identidade e escopo próprios. Um evento de execução desconhecido pode ser estacionado para reconciliação; não criar confiança em uma conta sem vínculo verificável. Fixtures pré-carregadas podem representar operações anteriores à API, desde que tenham histórico correspondente explícito.

### 7.3 Máquina de estados para acompanhamento de conta

Esta é uma modelagem do provider bancário/reputação; não adicionar um CRUD público no hackathon. A V3 consome evidências e restrições normalizadas, sem implementar uma equipe de investigação.

| Estado | Significado e efeito | Saídas permitidas |
|---|---|---|
| `UNKNOWN` | Sem evidência suficiente; não é suspeita nem atestado de limpeza | `OBSERVED`, `UNDER_REVIEW`, `RESTRICTED` mediante novo fato |
| `OBSERVED` | Dados utilizáveis sem restrição vigente; critérios normais continuam | `UNDER_REVIEW`, `RESTRICTED`; evidência vencida volta a desconhecida |
| `UNDER_REVIEW` | Alerta/relato aberto com origem, justificativa e prazo; sinal de risco, não bloqueio automático universal | `CLEARED`, `RESTRICTED`, novo prazo justificado |
| `RESTRICTED` | Restrição explícita aplicável à conta/pessoa e ao papel de pagador/recebedor; rejeita enquanto vigente | `CLEARED` por revogação autorizada; nova restrição pode substituir versão |
| `CLEARED` | Evidência específica cancelada ou investigação encerrada; manter auditoria | `OBSERVED` após recompor contexto, ou nova investigação por nova evidência |

Um score alto de nossa operação não altera automaticamente o estado global da pessoa. Isso evitará criar um ciclo em que o próprio motor marca alguém e usa sua marca como “prova externa” na operação seguinte. Cancelamento remove somente o efeito daquela evidência; não zera outras ocorrências válidas. Usar `source`, `subject`, `reason`, `case_id`, `valid_from`, `valid_until`, `revoked_at` e versão/evento de origem.

## 8. Persistência e concorrência na Base API

### 8.1 Tabelas mínimas

Definir migrations/DDL explícito. O `database.sql` do container é inicialização de ambiente novo, não mecanismo para evoluir banco existente.

| Tabela | Campos e constraints essenciais |
|---|---|
| `analysis_claim` | PK `(tenant_id, external_id)`; hash canônico permanente da primeira solicitação válida; estado `in_progress/completed`; dono, fencing token, lease_until; FK da análise ao concluir. Coordena IDs ainda em voo antes de efeitos externos |
| `pix_transaction` | PK UUID interna; `tenant_id`; `external_id varchar(128)`; `payload_hash char(64)`; payload canônico JSONB e bruto controlado; valor BIGINT >0; origem/destino normalizados; device/channel/IP normalizado; datas TIMESTAMPTZ; `received_at`; unique `(tenant_id, external_id)` nomeada `uq_pix_tenant_external_id` |
| `risk_analysis` | PK UUID que vira `key`; FK única para Pix; status com CHECK dos três valores; `decision_at`; sequência; versão de política; resposta pública JSONB; snapshot JSONB; scores/motivos; estado do dispositivo e `night_qualifies` originais. Uma análise por Pix, imutável |
| `source_initial_check` | PK/unique dos campos de `AccountKey`; estado `in_progress/completed`; dono da lease, fencing token crescente, `lease_until`, horários e resultado tipado JSONB. A inexistência representa `unclaimed` |
| `bank_event` | PK; tenant/provider/event_id unique; hash; tipo; sujeito; identificador da operação/desafio/dispositivo; occurred_at/received_at; versão de origem; payload normalizado; CHECK de estados; vínculo local quando conhecido |
| `request_receipt` | PK; tenant quando autenticado; request/trace ID; received_at; ID/hash quando extraíveis; resultado HTTP; referência ao Pix quando existe; categoria do erro. Registra entrada/replay/erro sem criar outra tentativa de risco |

`request_receipt` registra o mínimo necessário; não logar tokens ou payload integral de autenticação. Solicitações inválidas não viram transações aprovadas/rejeitadas fictícias. Se PostgreSQL estiver indisponível, registrar a falha no logger e retornar 503; não afirmar persistência garantida de uma requisição cuja gravação falhou. Não retornar 201 antes de commit confirmado.

Índices: conta pagadora+`transaction_date`, conta pagadora+`decision_at`, destino+`transaction_date`, eventos por sujeito/operação+ocorrência/conhecimento, inicial por estado/lease. Para consultas de janela, usar índices e parâmetros; não carregar todo o banco em cada request. O snapshot contém as features e evidências efetivamente usadas, não precisa copiar o histórico inteiro.

**Não é necessária uma terceira tabela de “todos os recebimentos”.** O fan-in pode consultar as tentativas de `pix_transaction` pelo destino; pagamentos efetivos exigem `bank_event`. Uma tabela agregada posterior é otimização/materialização com reconstrução possível, não nova fonte de verdade. Histórico de outros bancos fora da nossa visibilidade só existe se um provider fornecer esse dado.

### 8.2 Algoritmo transacional

1. Autenticar, registrar recebimento mínimo, validar/normalizar e calcular hash. Consultar por tenant/ID. Replay/conflito persistido encerra sem providers. Para ID ainda sem análise, reservar `analysis_claim` em transação curta, com lease de 10s e fencing token. Se já reservado com outro hash, devolver 409 antes de qualquer provider ou checagem inicial de conta. Mesmo hash em voo espera até concluir ou retorna 503 dentro do orçamento; não trabalha em paralelo com o dono. Lease vencida pode ser retomada mantendo o hash. Reserva de ID não é análise nem tentativa pontuada.
2. Somente o dono do ID reivindica a checagem inicial da origem em transação curta, usando chave única, lease de 10s e fencing token. Se outro dono está ativo para a conta, aguardar no máximo o orçamento total da requisição e então retornar 503; não pular a checagem como se estivesse concluída. Depois do vencimento, um novo dono pode finalizar a consulta inicial.
3. Montar contexto preliminar e obter dados externos fora de locks de contas. Orçamento inicial: 250ms por chamada, no máximo uma repetição técnica dentro do orçamento, 3s totais por requisição incluindo esperas. Encerrar tentativa de consulta inicial com estado de cobertura verdadeiro, inclusive indisponível; confirmar a posse antes de salvar resultado. Crash pode repetir uma leitura remota na retomada, mas não autoriza dois resultados locais.
4. Abrir a transação final em `READ COMMITTED`; adquirir locks de origem e destino em ordem global determinística. Na implementação proposta, usar advisory locks transacionais com IDs de 64 bits derivados de SHA-256 de chaves namespaced. Ordenar os **IDs numéricos efetivamente travados**, remover duplicados; colisão só reduz paralelismo, não altera identidade de registros. `lock_timeout` de 1s.
5. Reconsultar tenant/ID depois dos locks. Travar a linha de `analysis_claim` com `FOR UPDATE`, verificar dono/token e lease ainda válida; proprietário substituído não pode gravar. Relêr tentativas, execuções, exposição, checagem inicial e restrições locais. Verificar vigência das evidências externas em `decision_at`. Recalcular todas as features e `C_pre`.
6. Se o novo contexto exigir consulta ainda ausente/vencida, liberar a transação, consultar fora do lock e tentar novamente. Limitar a três ciclos e aos 3s totais. Se não estabilizar, retornar 503; nunca aprovar usando um contexto que sabidamente mudou.
7. Com contexto consistente, avaliar uma vez, inserir Pix/análise/DTO, marcar o claim concluído com referência à análise e finalizar receipt, tudo na mesma transação. `flush()` e `commit()` no escopo de tratamento de erro. Só retornar sucesso depois do commit.

GET só retorna análise concluída: enquanto houver apenas reserva, responde 404, sem criar um quarto status de risco. Uma reserva validada mantém o ID e hash mesmo após erro técnico; o cliente deve retomar com o mesmo corpo. O simulador sequencial não implementa leases nem concorrência: essas são obrigações da integração PostgreSQL. Testar takeover de claim de operação e de consulta inicial separadamente; não confundir os dois tokens.

O lock do destinatário é necessário porque `MULE_FAN_IN` altera decisão entre origens concorrentes. Somente lock por remetente não protege esse agregado. Qualquer ingestão de evento que mude exposição/perfil deve respeitar o mesmo lock da origem; mutações locais que afetem destino devem seguir o protocolo correspondente. A política carregada é imutável durante a análise.

O `UNIQUE` tenant/ID é a última defesa mesmo se origens diferentes usarem o mesmo ID simultaneamente. Capturar especificamente SQLSTATE `23505` + constraint `uq_pix_tenant_external_id`; fazer rollback, ler o vencedor e comparar hash. Não tratar todo `IntegrityError` como replay. O erro pode surgir no flush ou no commit. Deadlock, timeout e resultado de commit desconhecido são falhas técnicas; no último caso o cliente repete o mesmo ID/corpo para resolver pelo registro persistido. [SQLAlchemy: flush e transação](https://docs.sqlalchemy.org/en/20/orm/session_basics.html#flushing).

Não existe atomicidade absoluta com o estado de um banco externo que muda depois do snapshot. Registrar versão/frescor, minimizar a janela e manter o banco responsável por controles finais antes de executar. Não declarar que `READ COMMITTED` sozinho resolve concorrência; a consistência aqui depende do protocolo de locks e da revalidação.

### 8.3 Cache, threads e filas

Redis pode cachear evidências, snapshots e regras versionadas, respeitando escopo/TTL; não é necessário para começar. Não armazenar uma autorização sem validade nem fazer cache do resultado final por apenas CPF/valor. O PostgreSQL continua responsável pela idempotência e invariantes. Se usar Redis para agregados no futuro, definir atomicidade, reconstrução e comportamento em falha antes de substituir consultas corretas.

Não colocar a decisão síncrona exigida atrás de BullMQ. Fila pode servir para auditoria, atualização de materializações e reconciliação posterior, com entrega duplicada tolerada; o sucesso da resposta não pode depender de “persistiremos depois”. Threads ajudam I/O bloqueante da base, mas não substituem locks. Dimensionar workers/pool de conexões juntos; não criar uma thread por regra CPU simples nem executar SQLAlchemy síncrono diretamente no event loop sem adaptação.

## 9. Preparação para configuração low-code

Implementar um registro de critérios com identificador estável, família, função de avaliação, parâmetros e schema dos parâmetros. Cada avaliação retorna `Reason(code, family, points, floor)`; um agregador central aplica tetos e precedência. Um critério não grava no banco e não retorna sozinho a resposta HTTP.

Uma futura interface pode editar configuração JSON de critérios já disponíveis. O MVP carrega configuração validada no startup, por tenant e versão, a partir de arquivo ou registro persistido. Não precisa de editor, DSL genérica ou endpoint administrativo público. Não executar Python, SQL, expressões `eval` ou código enviado pelo cliente.

Configuração deve validar: regra conhecida; tipos; pesos não negativos; ordem de limiares (`two_fa_at < reject_at`); janelas positivas; percentuais em [0,100]; tetos definidos; pares de cortes ordenados. Restrições explícitas, autenticação, idempotência e limites operacionais não são desativados por um botão do cliente. Publicar nova versão afeta apenas análises novas; registrar a versão exata e testar cenários antes de ativar.

O `Policy` da referência fornece os parâmetros iniciais. Os pontos e algumas condições ainda estão no código: portar para o registro/configuração é trabalho da API, não uma interface low-code já implementada. Evitar prometer que qualquer regra nova poderá ser criada sem código: parâmetros são configuráveis; um novo tipo de dado/critério exige implementação e testes.

## 10. Extensões deliberadamente inativas na V3

* **Renda estimada:** pode compor capacidade/exposição, com fonte, confiança, validade e tratamento de PJ, sem presumir que transferência alta em relação à renda é fraude. Não há regra de renda no score atual.
* **IP versus GPS:** pode ajudar em combinação com aparelho novo/recuperação; VPN, operadora e precisão geográfica impedem equivalência simples. Consultar geolocalização externa somente quando a regra precisar; dado ausente não significa localização divergente. Não está ativo.
* **App versus internet banking:** é viável parametrizar sensibilidade, mas o canal sozinho não certifica registro. A V3 usa estado real/simulado do dispositivo e limite efetivo, sem liberar valor ilimitado no app. Mensagem “use o app” deve ser um código/motivo que o banco traduz em UX; não criar quarto status.
* **Ticket de CNPJ e perfil comercial:** pode reduzir falsos positivos por comparação com atividade confirmada, mas muitas operações no ticket habitual não tornam um esquema legítimo. Não desativar frequência/reputação por isso.
* **Biometria, liveness, SIM swap e integridade da sessão:** enriquecer por provider quando houver fonte; não presumir que cadastro aprovado há meses prova a operação atual.
* **Confiança contextual pós-2FA:** exige autorização bancária explícita com escopo/validade; não há regra de “o próximo request certamente passou o desafio anterior”.

## 11. Testes e sequência para implementar

### 11.1 Referência executável desta entrega

```bash
python3 -m venv .venv
.venv/bin/python -m pip install jsonschema==4.17.3
.venv/bin/python -m unittest -v test_contrato_pix.py test_politica_pix.py
.venv/bin/python politica_v2_simulacao.py
```

Também é possível usar o ambiente de revisão já existente em `tmp/revisao-v2/.venv`. O motor puro usa stdlib; a integração com normalização usa `jsonschema`, na versão da base. [politica_simulacao_resultados.json](politica_simulacao_resultados.json) é uma demonstração de cenários, não o oráculo dos testes nem evidência de eficácia real.

Os testes de aceitação da referência verificam dinheiro exato; DV e datas; replay/conflito cobrindo todo o payload; chamadas condicionais; desconhecido versus limpo; tentativas distintas; dispositivo não autorizado; limites e precedência; maturidade; janela temporal; evento conhecido tardiamente; 2FA separado de execução; limites por ambos os relógios; pagamento recorrente e folha PJ. O adapter de demonstração conserva o hash completo, mas seu `Pix` reduzido não substitui persistir todos os campos canônicos e `data_quality` na API.

Validação desta entrega: **36 testes de contrato + 56 testes do motor = 92 testes aprovados**. A simulação gera 59 observações determinísticas. A [revisão independente](REVISAO-INDEPENDENTE-GUIA.md) registra também as reproduções realizadas fora da suíte e o que ainda não foi testado.

### 11.2 Obrigatórios antes de declarar a API pronta

| Grupo | Evidência esperada |
|---|---|
| HTTP | Exemplo oficial aceito; três status; topo exato; GET pelo ID; autenticação e erros no padrão da base |
| Idempotência | Repetição igual não chama provider nem duplica registro; mudança em qualquer campo relevante dá 409; tenants isolados |
| Concorrência | Duas chamadas mesmo ID/hash só têm um dono; outro hash em voo dá 409 antes de providers; takeover e fencing; IDs distintos mesma origem; origens distintas mesmo destino perto do limiar de fan-in; chegada de evento durante análise |
| Transação | Falha entre inserts não deixa Pix sem análise; erro de flush/commit tratado; commit desconhecido resolvido por retry igual |
| Checagem inicial | Duas primeiras chamadas concorrentes; crash e takeover da lease; fencing rejeita dono antigo; indisponibilidade não vira dado limpo |
| Tempo | Noite atravessa meia-noite; fim da janela; histórico futuro excluído; evento atrasado; revogação atual em request com data antiga; timestamps iguais |
| Provider | Timeout, fixture ausente, dado vencido, sujeito errado, cache segregado, restrição conhecida apesar de `C_pre` alto |
| Persistência | Reabrir processo e obter mesma análise; replay não pontua; consultas usam índices; migrations em banco vazio e existente |
| Segurança de eventos | Evento duplicado idêntico, duplicado conflitante, desafio errado, transição terminal inválida, origem não autenticada |

Executar esses testes com PostgreSQL real separado de dados pessoais. Nenhum teste sequencial em memória comprova que advisory locks, migrations, middleware ou recuperação de commit funcionam na API. Não afirmar que a validação de outra LLM em outro ambiente cobriu o projeto local.

### 11.3 Ordem de entrega

1. Importar a Base API, preservar seus padrões e confirmar execução/health local.
2. Adicionar migrations, entidades, contratos/DTO e POST/GET com idempotência persistida.
3. Portar tipos e regras puras; plugar providers de fixtures determinísticos com versões identificadas.
4. Implementar eventos, checagem inicial, locks e revalidação; completar testes de integração.
5. Documentar comandos, variáveis sem segredos, códigos de erro, escopo dos mocks, regras ativas e limites no README.
6. Preparar demonstração de: primeira transação; troca legítima de aparelho; rajada com IDs distintos; noite acumulada; destinatário adverso; replay; 2FA com execução posterior.

Usar valores e identidades sintéticas. A entrega do hackathon é repositório e apresentação; não fazer deploy ou conectar sistemas bancários reais para validar esta proposta.

## 12. Referências e limites regulatórios

O desenho deve ser apresentado como antifraude de demonstração, sem certificar cumprimento integral do Regulamento Pix. Limite operacional, cadastro de aparelho, análise de fraude, autenticação adicional e bloqueio cautelar são mecanismos distintos.

* [Segurança do Pix — BCB](https://www.bcb.gov.br/estabilidadefinanceira/pix-seguranca): contexto de controles e responsabilidades.
* [Resolução BCB 142, texto vigente](https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?numero=142&tipo=Resolu%C3%A7%C3%A3o+BCB): regras de limites e suas hipóteses. Os nossos cortes próprios não substituem os limites definidos pelo participante, as exceções aplicáveis e os procedimentos regulatórios.
* [IN BCB 491](https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?numero=491&tipo=Instru%C3%A7%C3%A3o+Normativa+BCB) e [IN BCB 594](https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?numero=594&tipo=Instru%C3%A7%C3%A3o+Normativa+BCB): cadastro de dispositivos e limites correspondentes. Um piso de 2FA em nosso simulador não implementa sozinho esse cadastro e não permite superar um limite vigente.
* [API DICT](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/API-DICT.html) e [dúvidas da API](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/duvidas_comuns_api_v2.html): operações, estatísticas, escopo e definições dos dados.
* [Manual de Tempos do Pix](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/Regulamento_Pix/IX_ManualdeTemposdoPix.pdf): não confundir prazos operacionais e autorização do pagador com retenção pelo recebedor. Nosso orçamento de 3s é escolha de engenharia.
* [Receita Federal: primeiro CNPJ alfanumérico](https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/julho/receita-federal-gera-o-primeiro-cnpj-em-formato-alfanumerico) e [manual do DV](https://www.gov.br/receitafederal/pt-br/centrais-de-conteudo/publicacoes/documentos-tecnicos/cnpj/manual-dv-cnpj.pdf): justificam aceitar o formato além do CNPJ numérico.

## 13. Hipóteses incompatíveis e o que foi retirado do original

Esta seção fica no final para preservar o registro sem misturá-lo às instruções de implementação vigentes.

| Hipótese anterior / sugestão do complemento | Decisão desta versão |
|---|---|
| Consultar DICT de remetente e destinatário em todo Pix e remover `C_pre` | Não adotado: preservada checagem inicial por conta e consulta condicional do destinatário |
| CPF sem fixture representa pessoa limpa com contadores zero | Retirado: `not_covered` é ausência de cobertura; zero confirmado exige fixture `available` |
| Aprovação do motor ou 2FA aprovado prova pagamento | Retirado: perfil exige evento de execução; exposição segue trilha separada |
| Repetições de mesmo destino/valor em dois minutos são duplicatas | Retirado: somente tenant/ID/hash define replay |
| Qualquer aparelho visto em tentativa anterior é conhecido/confiável | Retirado: registro/estado vem do provider; observação não autoriza |
| App dispensa análise de valor; cadastro/liveness e crédito bons eliminam risco | Retirado: canal não comprova registro; crédito e saldo são controles bancários separados |
| Beneficiário recorrente ganha bônus negativo ou é automaticamente cadastrado | Retirado: recorrência habilita referência de valor; confiança bancária exige designação explícita |
| Transferência para mesmo CPF divide o risco ao meio | Retirado: conta própria nova continua sujeita a critérios e patamar noturno |
| Duas instituições reportando fraude equivalem a restrição oficial | Retirado: contagem é evidência; restrição exige fonte, escopo e validade explícitos |
| Histórico ilimitado no simulador e 90 dias no banco | Corrigido: janela única de 90 dias com maturidade e disponibilidade temporal |
| Referência por destinatário depende também de cinco pagamentos gerais | Corrigido: duas execuções maduras na mesma conta habilitam referência específica |
| `m12` equivale a 365 dias corridos incluindo mês atual | Retirado: fixtures agregadas; qualquer cálculo futuro deve seguir a definição oficial do campo |
| Cache antigo de consulta inicial prova situação atual limpa | Retirado: preservar histórico sem inventar validade; restrições atuais vêm do contexto bancário |
| `READ COMMITTED` por si só resolve rajada, fan-in e idempotência | Retirado: locks de origem/destino, constraints, transação e revalidação são obrigatórios na API |
| Todo `IntegrityError` significa chamada duplicada | Retirado: tratar somente a constraint de idempotência como replay |
| Banco libera limite noturno apenas porque passou 2FA | Retirado: desafio não supera limite operacional; cortes da demo são política própria |
| Muitas requisições PF devem ser todas negadas e PJ só desafiadas | Não adotado: contexto e critérios combinados, com limites explícitos para ambos |
| É preciso implementar microserviços, BullMQ, Redis, editor visual e webhook público | Retirado do MVP: interfaces internas e fixtures bastam; fila/cache são extensões |
| Testes/simulações da outra LLM comprovam integração PostgreSQL local | Não adotado: só declarar as verificações realmente executadas nesta entrega |

Os originais recebidos estão preservados em [revisao-v2/originais](revisao-v2/originais). A [revisão anterior](REVISAO-AVALIACAO-ANTIFRAUDE-PIX-v2.md) documenta o diagnóstico histórico; seus bugs reproduzidos não devem ser usados como expectativas para o motor V3.
