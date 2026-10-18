# Contrato, motor e política experimental Pix — PR 2

Esta etapa entrega uma biblioteca utilizável e o carregamento da política no
startup da Base API. Não publica endpoints Pix. O fluxo HTTP depende de
providers, storage, claims, locks e transação das próximas etapas do plano.
Não há nova migration ou nova dependência nesta PR.

## Interfaces e fronteiras

- `antifraud.contract.normalize_json(raw: bytes)` lê JSON com `Decimal` antes
  de validar centavos; rejeita duplicatas, NaN/Infinity, NUL e surrogates.
  `normalize_payload` atende objetos Python já construídos; não recupera
  precisão perdida por um `json.loads` comum. No futuro resource, usar bytes.
- Schema oficial e verificações semânticas validam CPF/CNPJ (inclusive
  alfanumérico), UTC, aberturas, ID e metadados. IP inválido gera `IP_INVALID`
  e `null`, sem pontos próprios; o texto original afeta o hash e é retido em
  `NormalizedPayload.invalid_ip_raw`, apenas na auditoria privada.
- Hash SHA-256 usa todos os campos normalizados, inclusive campos que não
  pontuam. Não garante idempotência sozinho; reserva e persistência são futuras.
- `antifraud.adapter.to_pix(normalized, tenant)` constrói a identidade do
  motor. O tenant é parâmetro do servidor; nunca vem de um campo extra do JSON.
  O payload completo é mantido no objeto normalizado para hash/auditoria;
  somente os campos usados pelas regras são projetados para `Pix`.
- `antifraud.engine.evaluate(Context, Policy)` devolve `Decision` imutável.
  Entradas são dataclasses congeladas e tuplas, com tempo informado pelo
  chamador. Sem banco, rede, geração de UUID ou leitura de relógio global.
- `preliminary` executa a mesma avaliação sem evidências DICT; `local_score`
  e `c_pre` são iguais aos da avaliação final sobre o mesmo contexto local.
- `antifraud.dto.PixTransactionDTO.from_decision(key, normalized, context, policy, decision)`
  formata uma `Decision` já calculada pelo chamador em `public_json`/`audit_json`
  congelados, sem executar regras. UUID e tempo também vêm do chamador.
  O chamador deve fornecer a decisão do mesmo Context/Policy; o factory recusa
  Pix diferente do payload validado e versão de decisão diferente da política.
  A biblioteca fica em `antifraud` para não importar o package de DTOs legados, que carrega
  models/SQLAlchemy. O resource futuro continuará usando a camada de DTO.

O DTO público tem exatamente `key`, `status`, `metadata`. Metadata inclui
versão, score, coeficiente preliminar, motivos, tempo, qualidade e ações/estados
de enriquecimento. Não inclui documentos de terceiros, contadores de fraude
ou prontuário de bureau. Para consulta não solicitada, `state` é `null`;
`action` distingue skip/cache/consulta quando fornecida pelo orquestrador.
Não fabricar `not_covered` quando não houve consulta. Uma ação explícita deve
concordar com `dict_queried`; `cache` exige evidência vigente do papel. Ações
omitidas podem ser inferidas de `dict_queried`, sem contradizer uma ação explícita.

`Pix.device_id` e `DictEvidence.provenance` são obrigatórios, sem defaults de
fixtures nos tipos de produção. Fixtures passam a origem explicitamente nos
testes. Restrições de `payer` aceitam `AccountKey`; de `payee`, `DestinationKey`.
`both` significa restrição ao documento nos dois papéis e exige `account=None`.
Para restringir uma conta específica, informar seu papel e o tipo correspondente.

O registro privado guarda política integral/fingerprint, decisão com pontos,
tetos e pisos, payload/hash, evidências de segurança/DICT e features agregadas
usadas pelas regras. Medianas de centavos são frações `{numerator, denominator}`,
sem float. `invalid_ip_raw` completa o envelope canônico privado
`{"payload": ..., "invalid_ip_raw": ...}` para recomputar o SHA-256, inclusive
quando o IP normalizado é `null`. Não copia todo o histórico. As propriedades
`public` e `audit` retornam cópias independentes. Imutabilidade aqui é em memória; proteção de
banco e replay HTTP serão implementados nas etapas 4/5.

## Configuração

`src/antifraud/policies.json` contém `policies` por versão e `tenants` apontando
para uma versão existente. A configuração inicial só contém `demo` e
`pix-experimental-v3`, com os defaults exatos do simulador V3. Não resolve
credenciais nem faz fallback entre instituições.

Cada versão exige todos os parâmetros globais e todos os 25 critérios. Cada
critério tem identificador, família, avaliador puro, defaults e schema de
parâmetros, expostos pelo registro somente leitura `CRITERIA`. Não há flags
para remover regras, DSL, `eval`, SQL ou endpoint administrativo.

O startup valida regras/campos conhecidos, tipos estritos (bool não é inteiro),
pesos e tetos não negativos, cortes ordenados, janelas positivas, percentuais
em [0,100], horas noturnas cruzando meia-noite e referências de tenants. JSON
com chave duplicada ou constante não finita é recusado. A carga cria cópias
imutáveis; alterar o dicionário original não muda uma análise. Sem hot reload.

Limite técnico adicional da API: janelas configuráveis limitadas a um ano
(365 dias, 8.760 horas ou 525.600 minutos; TTL até 31.536.000 segundos). Isso
preserva a margem aritmética do contrato de ano UTC 0002–9998, inclusive nos
extremos de offsets. Não altera os defaults e não é exigência do enunciado ou
calibração de risco. Cálculos de duração usam UTC; dia/noite usam
`America/Sao_Paulo`. Frações e limites monetários permanecem exatos.

Qualquer mudança deliberada deve receber nova versão e ser validada antes de
ativar. O fingerprint inclui a versão e todos os parâmetros, em JSON canônico;
registra a configuração exata mesmo se um operador reutilizar um nome por erro.
Uma mesma instância carregada não pode mudar durante a análise.

## Critérios

A coluna de parâmetros lista os pontos configuráveis; `{}` indica somente
condição/piso definido no código. Cortes/janelas vêm de `Policy`, declarados
integralmente no arquivo JSON e validados por `configuration_schema()`.

| Critério | Família | Pontos iniciais | Fase |
|---|---|---|---|
| `NIGHT_SOFT_LIMIT` | Limite | `{}` | local |
| `NIGHT_HARD_LIMIT` | Limite | `{}` | local |
| `LOW_HISTORY_EXPOSURE` | Limite | `{'low_points': 15, 'high_points': 30}` | local |
| `VELOCITY_BURST` | Velocity | `{'points': 30}` | local |
| `RECEIPT_VELOCITY` | Velocity | `{'points': 30}` | local |
| `DAILY_AMOUNT` | Montante | `{'points': 20}` | local |
| `STRUCTURING` | Montante | `{'points': 30}` | local |
| `AMOUNT_ABOVE_PROFILE` | Perfil | `{'low_points': 15, 'high_points': 30}` | local |
| `RECENT_PAYEE_ACCOUNT` | Contraparte | `{'young_points': 25, 'recent_points': 10}` | local |
| `NEW_PAYEE` | Contraparte | `{'points': 10}` | local |
| `SELF_TRANSFER_NEW_ACCOUNT` | Contraparte | `{'points': 15}` | local |
| `MULE_FAN_IN` | Contraparte | `{'pf_points': 40, 'pj_points': 20}` | local |
| `DEVICE_REVOKED` | Acesso | `{}` | local |
| `UNREGISTERED_DEVICE` | Acesso | `{'points': 20}` | local |
| `DEVICE_CHANGE` | Acesso | `{'points': 20}` | local |
| `DEVICE_POLICY_LIMIT` | Acesso | `{}` | local |
| `RECENT_ACCOUNT_RECOVERY` | Acesso | `{'points': 25}` | local |
| `PAYER_BANK_RESTRICTED` | Contraparte | `{}` | local |
| `PAYEE_BANK_RESTRICTED` | Contraparte | `{}` | local |
| `BANK_EFFECTIVE_LIMIT` | Limite | `{}` | local |
| `PAYER_DICT_ADVERSE` | Contraparte | `{'points': 30}` | reputation |
| `PAYER_DICT_RESTRICTED` | Contraparte | `{}` | reputation |
| `PAYEE_DICT_ADVERSE` | Contraparte | `{'points': 30}` | reputation |
| `PAYEE_DICT_RESTRICTED` | Contraparte | `{}` | reputation |
| `DICT_CHECK_UNAVAILABLE` | Contraparte | `{'points': 10}` | reputation |

Os tetos iniciais são Limite 30, Velocity 30, Montante 30, Perfil 30,
Acesso 40 e Contraparte 50. Pesos assimétricos mantêm o máximo entre os dois
relógios de frequência; o segundo motivo recebe somente o incremento positivo.
Os papéis DICT adversos do mesmo documento também contribuem com o máximo,
sem duplicar; documentos distintos continuam independentes. `score >= 70`
rejeita; `score >= 30` exige
2FA. Pisos prevalecem sobre score e tetos; zerar pesos não remove uma
restrição explícita ou um limite operacional. `C_pre` é coeficiente ordinal,
não probabilidade; seleção condicional de consultas pertence à PR 3.

A análise não é execução: apenas evento bancário `executed` conhecido e
ocorrido até `decision_at` forma perfil de pagamentos; maturidade usa também
o horário da execução. Falha terminal do desafio correto ou execução
cancelada/falha remove exposição segundo a referência. Conhecimento tardio,
futuro, sujeito/tenant e validade são filtrados explicitamente. O motor não
ingere/valida transições bancárias nem escolhe a versão mais nova do provider:
recebe snapshots já selecionados, trabalho das etapas 3/4.

## Diferenças deliberadas da referência

A referência é um simulador, não a especificação de integração. O port mantém
as decisões completas dos 59 cenários e dos 2.000 contextos diferenciais.
Há duas bordas adicionais, cobertas por testes próprios:

- Papel em `dict_queried` sem evidência correspondente soma
  `DICT_CHECK_UNAVAILABLE` uma vez. A referência omite esse sinal nessa entrada
  inconsistente; o guia §5.3 exige tratar consulta necessária indisponível como
  desconhecimento, nunca limpeza. Nenhum cenário arquivado depende da omissão.
- Aritmética de duração é feita em UTC, conforme guia §4.2, para evitar overflow quando um instante
  de ano UTC 0002 tem representação local no ano 0001. O horário de negócio
  continua em São Paulo. Também preserva horas decorridas ao atravessar mudanças
  históricas de horário de verão, em vez de subtrair horas no calendário local.

Tipos/enums/contadores e configuração agora falham cedo em entradas inválidas,
e objetos de payload/DTO não expõem estado mutável. Não se atribuem essas
validações aos testes de integração HTTP ou aos providers ainda futuros.

## Validações de borda

Hora/minuto/segundo são validados explicitamente antes de `fromisoformat`,
preservando 422/QIT002004 mesmo em runtimes que aceitem `24:00:00`.
