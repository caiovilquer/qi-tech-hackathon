# Ajustes após o enunciado oficial do hackathon

Análise de 17/10/2026. Fonte: [Hackathon Antifraude Pix.pdf](<Hackathon Antifraude Pix.pdf>), 28 páginas; contrato e escopo nas páginas 19–25, agenda na página 27. Texto extraído integralmente; páginas 19–22 e 24–25 conferidas visualmente.

Este documento registra a comparação feita quando o enunciado chegou. Os ajustes já foram incorporados à [análise principal consolidada](ANALISE-ANTIFRAUDE-PIX.md), que passa a ser a referência atual e autossuficiente do projeto. Ainda não há implementação. Não foi necessário refazer a pesquisa regulatória: a mudança principal vem do contrato e das restrições de escopo do evento.

**Atualização posterior, 17/10/2026:** segundo orientação da organização relatada pelo usuário, sinais externos podem ser simulados e ideias probabilísticas são permitidas. As restrições abaixo a payload/histórico e a mocks refletem a comparação inicial; consulte a análise principal, especialmente as seções 3.1, 6.1 e 16.5, para a proposta atual. O contrato público permanece o mesmo.

## 1. Conclusão

O desenho de critérios plugáveis continua adequado, mas o produto fica menor e mais específico: uma API que analisa e registra solicitações usando o payload recebido e o histórico local. Não precisamos construir um banco, cadastrar contas, executar Pix, realizar 2FA ou integrar serviços externos.

O diferencial deve estar na qualidade das regras e de suas combinações, no tratamento de pouco histórico, na modelagem e nos testes. A banca verifica se o comportamento implementado corresponde ao racional descrito no README.

## 2. Requisitos confirmados pelo PDF

| Item | Exigência / referência |
| --- | --- |
| Entrada | `POST /pix/transaction`, conforme payload das páginas 19–20 |
| Consulta | `GET /pix/transaction/{id}`, recuperando a análise gravada, página 21 |
| Saída | Exatamente três campos de topo: `key`, `status`, `metadata`, página 21 |
| Status | `approved`, `rejected`, `two_factor_required`, páginas 16 e 21 |
| Motivo | Toda decisão deve ser explicável, página 16; `metadata` é o espaço livre para isso |
| Persistência | Banco próprio, histórico de tudo que chegou e Pix gravados/consultáveis, páginas 22 e 25 |
| Base | Partir do Base Service e seguir os padrões do bootcamp, páginas 22–23 |
| Testes e documentação | Regras, testes e README, páginas 22–25 |
| Fora do escopo | Onboarding, cadastro de contas, frontend e outras APIs, página 22 |
| Apresentação | Até sete minutos, mais três de perguntas, páginas 23 e 26 |
| Entrega | Link do repositório até domingo, 18/10/2026, às 12h, no grupo da final no Slack, página 27 |

O PDF cita decidir em poucos segundos, mas não fixa SLA numérico ou volume de teste. Também não especifica pesos da rubrica, dataset rotulado ou taxa mínima de detecção.

As páginas iniciais apresentam produtos da QI Tech, incluindo no-code, biometria, KYC e crédito. Isso é contexto da empresa; não é exigência para construir editor de regras, reconhecimento facial ou análise de crédito neste desafio. O documento RFC não é entregável obrigatório: a página 23 pede o processo de discussão e definição de escopo.

## 3. Contrato: o que agora está fechado

### Entrada

| Campo | Significado e tratamento proposto |
| --- | --- |
| `id` | ID único do Pix, textual; não confundir com UUID da análise |
| `amount` | Inteiro em centavos; o exemplo `150000` representa R$1.500 |
| `transaction_date` | Data e hora com fuso, referência temporal da transação |
| `source_account.branch`, `account_number` | Identificação da conta de origem; preservar zeros como texto |
| `source_account.opening_date` | Abertura da conta de origem; permite calcular sua idade |
| `source_account.owner.document_number` | CPF ou CNPJ do pagador |
| `destination_account.participant` | ISPB do banco de destino, textual |
| Demais campos de `destination_account` | Agência, conta, abertura e CPF/CNPJ do recebedor |
| `source.device_id` | Identificador observado do aparelho; não comprova cadastro, integridade ou posse legítima |
| `source.channel`, `source.ip` | Canal e IP informados; canal não tem enumeração fechada no PDF |
| `source.geolocation` | Latitude/longitude opcionais; ausência não é evidência de fraude |

Não acrescentar como obrigatórios `customer_id`, `account_id`, `device_registered`, saldo, moeda ou resultado de KYC: o contrato não os fornece. Interpretar BRL como hipótese do domínio Pix, sem exigir um campo `currency` novo. O esquema completo de limites de tamanho, nulls, campos extras e erros HTTP não está publicado nos slides.

### Saída e consulta

Usar os status com a grafia exata. `key` identifica a análise; o UUID mostrado é um bom formato a adotar, mas o texto do slide não define formalmente seu tipo. Interpretar `{id}` do GET como o ID do Pix enviado no POST, conforme a nomenclatura apresentada, e documentar essa decisão.

Exemplo proposto, compatível com o envelope oficial; campos internos de `metadata` são escolha nossa:

```json
{
  "key": "b3f1c2e0-7d4a-4c1e-9a52-1f0e6c9d2a77",
  "status": "two_factor_required",
  "metadata": {
    "policy_version": "1.0.0",
    "reason_codes": ["FIRST_SEEN_DEVICE", "NEW_COUNTERPARTY"],
    "reason": "Aparelho ainda não observado para este pagador e primeira transferência observada para este destino.",
    "history_mode": "limited"
  }
}
```

O GET devolve a análise persistida, sem recalcular com histórico ou configuração mais recentes. Repetição do POST não deve gerar uma análise diferente inadvertidamente.

## 4. O que muda em relação à proposta anterior

| Proposta exploratória | Ajuste após o PDF |
| --- | --- |
| Contrato pendente | Rotas, campos e envelope definidos; faltam apenas semânticas de borda |
| PF como recorte inicial | Entrada aceita CPF ou CNPJ; evitar calibrar tudo como transferência entre PF |
| Desafio 2FA com expiração, tentativas e confirmação | Retornar `two_factor_required`; ciclo completo não é solicitado |
| Consulta de reputação/DICT e conectores | Retirar do caminho crítico e da entrega; outras APIs estão fora do escopo |
| Estado de dispositivo cadastrado/revogado | Usar somente familiaridade observada por pagador; não inventar estado bancário |
| Limites regulamentares efetivos | Sem dados de cadastro, configuração de limites e modalidade, não alegar implementação integral; usar política própria explicitamente identificada |
| Perfil de pagamentos liquidados | Perfil de solicitações observadas, com ressalva de seleção/contaminação; liquidação não é informada |
| Eventos de confirmação e investigação | Não criar callbacks extras; sem rótulos reais, não anunciar precisão/recall reais |
| Dashboard ou interface como evolução | Retirar da entrega: frontend está expressamente fora do escopo |
| RFC como possível documentação | README é obrigatório e central; RFC separado é dispensado |

**Correção principal:** a regra de ausência de fonte crítica da pesquisa anterior não se aplica a fontes que este produto nunca receberá. Não podemos pedir 2FA ou rejeitar todo mundo porque não há DICT, saldo ou KYC no contrato. O motor precisa funcionar com os sinais que efetivamente possui.

O mesmo vale para os exemplos de R$200/R$1.000 de dispositivo: aparelho nunca visto na nossa base não equivale a aparelho não cadastrado no PSP. Não aplicar esses tetos automaticamente por `FIRST_SEEN_DEVICE`.

## 5. Critérios que agora fazem sentido

| Critério | Dados disponíveis | Leitura recomendada / cuidado |
| --- | --- | --- |
| Valor absoluto / exposição inicial | `amount` | Política própria e contextual, sem alegar limite bancário efetivo |
| Frequência em janelas curtas | Documento do pagador, IDs distintos, datas | Captura rajadas; não contar replay do mesmo ID como novo Pix |
| Montante acumulado | Valores e datas do histórico | Detecta fracionamento; chamar de montante solicitado, não dinheiro gasto |
| Desvio do perfil observado | Valores anteriores do pagador | Usar estatística robusta somente com amostra suficiente |
| Novo destinatário para o pagador | Relação origem → conta destino | É sinal fraco sozinho: novos pagamentos são normais |
| Idade da conta recebedora | Abertura e data da transação | Conta recente aumenta incerteza; não prova fraude |
| Idade da conta pagadora | Abertura e data da transação | Distinguir conta recém-aberta de cliente antigo novo na nossa base |
| Aparelho ainda não observado | Documento do pagador + `device_id` | Familiaridade por pagador; não considerar confiável porque outro usuário o usou |
| Mudança de canal/IP | Histórico de `channel` e `ip` | IP móvel/NAT muda legitimamente; não rejeitar só por isso |
| Deslocamento incompatível | Duas geolocalizações e intervalo positivo | Opcional, com tolerância e amostra confiável; dado informado não é atestado antifalsificação |
| Muitos pagadores para um destino recente | Destino, documentos distintos, janela | Indício contextual; comerciantes legítimos também recebem de muitos pagadores |

Para o MVP, priorizar frequência, montante, perfil, novo favorecido, idade das contas e familiaridade de aparelho. Geolocalização pode ser o critério adicional se houver tempo para tratar ausência, ruído e empates temporais corretamente.

Não há base para implementar recuperação recente de senha, SIM swap, biometria, aparelho comprometido, saldo esvaziado ou marcação oficial de fraude. A primeira análise continha essas possibilidades; agora elas ficam explicitamente fora do conjunto executável.

No início, não é necessário criar lista de bancos “arriscados” por ISPB. O campo identifica a instituição para distinguir contas; não demonstra risco por si só. Risco de contraparte também não pode ser calculado como “quantas vezes nosso próprio motor a rejeitou” e depois usado como confirmação de fraude: isso criaria um ciclo de reforço sem evidência independente.

## 6. Política inicial e composição de regras

Há três conceitos diferentes:

1. **Conta nova:** idade calculada por `opening_date` e `transaction_date`.
2. **Pagador sem histórico local:** ainda não observado pelo serviço, mesmo que sua conta exista há anos.
3. **Aparelho novo para o pagador:** não observado na combinação daquele documento e dispositivo.

Na primeira solicitação, tanto aparelho quanto destinatário serão desconhecidos localmente. Somar automaticamente os dois como evidências independentes fortes pode fazer toda primeira transação cair em 2FA. Tratar essas novidades como parte do contexto de pouco histórico, com contribuição conjunta limitada ou condicionada à exposição.

Proposta revisada, ainda sujeita à calibração:

- Primeira solicitação de valor baixo, conta de origem antiga e sem anomalia forte: permitir aprovação.
- Pouco histórico com exposição maior, destino recente ou mudança relevante de acesso: pedir 2FA.
- Rajada e montante elevados, combinados com contraparte nova/recente ou desvio muito forte: permitir rejeição por risco composto documentado.
- Conta nova, IP diferente, localização ausente ou novo destinatário, isoladamente: não classificar como fraude.
- Para mesma titularidade, reduzir a interpretação de “favorecido novo”, mas manter controles de frequência e exposição. Mesmo documento não torna toda operação segura.

Os números de pesos e limiares da pesquisa original eram ilustrativos. Não devem ser transplantados sem revisão: perderam alguns sinais, ganharam idade de contas e passaram a usar um histórico de solicitações sem confirmação de liquidação.

Manter a separação entre regras e política. Cada regra produz evidência e contribuição; a política combina famílias de sinais para evitar dupla contagem. Um resultado de regra pode impor uma ação quando houver justificativa explícita, mas os impedimentos normativos/externalizados antes discutidos não existem magicamente neste payload.

## 7. Histórico: persistir tudo sem chamar tudo de sucesso

O enunciado confirma banco próprio e armazenamento de Pix analisados em qualquer status, inclusive recusados e com 2FA. Não há confirmação de execução, pagamento liquidado ou fraude real. Portanto:

- Usar todas as intenções distintas válidas para detectar repetição e abuso.
- Manter agregados separados de solicitado, aprovado pelo motor, recusado e 2FA, quando úteis.
- Para perfil, declarar qual subconjunto é usado. Uma proposta é usar solicitações anteriormente aprovadas como referência comportamental provisória, com limites robustos e diversidade temporal; isso continua sendo uma amostra selecionada pelo próprio motor, não histórico legítimo comprovado.
- Persistir todos os resultados mesmo quando não participarem da mediana do perfil.
- Não expor “total pago”, “saldo”, “fraude confirmada” ou “reputação limpa” sem informação que os comprove.

“Tudo que chegou” não define como guardar JSON inválido ou tentativas HTTP conflitantes. Proposta: cada Pix válido e cada análise ficam no banco; retries, conflitos e falhas de validação têm registro de recebimento separado, sem criar novas transações de negócio. Dados inválidos e logs devem ter limites e tratamento de privacidade. Confirmar com a organização se esperam consulta de corpos inválidos pelo GET; não fabricar um Pix quando o corpo sequer tem ID utilizável.

### Modelo mínimo revisado

Uma tabela de transações com ID externo único, dados normalizados para consulta, snapshot da entrada e instante de recebimento; uma tabela de análises em relação 1:1, com `key`, status, metadata, versão da política e evidências das regras em JSONB. Se for necessário rastrear todos os recebimentos, uma terceira tabela pequena registra retry/conflito/erro e sua correlação.

Não precisamos de tabelas de desafio, saldo, lançamentos, KYC, MED ou confirmação de liquidação. Dados de contas são atributos observados da solicitação, não um cadastro bancário operável.

Identidade do destino: ISPB + agência + conta; documento do titular permite agregar também por recebedor. Na origem não há ISPB: adotar explicitamente o escopo de uma instituição remetente por instância/integrador, com documento + agência + conta como chave local. Não inventar um ISPB de origem nem colapsar documentos distintos apenas por número de conta. Mudanças de titularidade/atributos ao longo do tempo devem preservar os snapshots originais.

## 8. Tempo, idempotência e concorrência

**Tempo:** usar `transaction_date` para idade das contas e janelas comportamentais, preservando `received_at` do servidor para auditoria. Comparar tudo como instantes com fuso; documentar separadamente o fuso escolhido para períodos locais. Não rejeitar automaticamente o exemplo do slide por estar à noite do próprio dia enquanto o teste roda à tarde. O PDF não define política de atraso/futuro nem garante eventos ordenados.

Para evento atrasado, usar somente registros já disponíveis cuja data não seja posterior à transação avaliada. Não recalcular decisões antigas no GET. A mesma requisição, snapshot e versão da política devem produzir resultado estável. Para timestamps iguais, usar uma ordenação estável de recebimento/serialização; não simplesmente excluir todos os registros com data igual, pois isso eliminaria rajadas com o mesmo timestamp. Geolocalização com intervalo zero não permite calcular velocidade.

**Idempotência proposta:** `id` igual e conteúdo canônico igual recupera a mesma análise; `id` igual com conteúdo diferente gera conflito, preservando o registro original. Não exigir um novo cabeçalho `Idempotency-Key` para compensar um ID já definido no contrato. Regras de erros, códigos HTTP e namespace do ID precisam ser documentados como decisões nossas, pois os slides não os definem.

**Concorrência:** serializar a leitura do agregado e a gravação de uma nova intenção no escopo necessário. Um lock por pagador cobre regras por pagador, mas não resolve automaticamente regras agregadas do destinatário. Se incluirmos um limite rígido de contraparte, coordenar também essa chave, em ordem determinística. Restrições únicas protegem IDs contra duplicidade. Nenhuma consulta externa é necessária dentro da transação.

## 9. Arquitetura e Base Service

Manter a estrutura da primeira fase e os padrões do Viazio:

```text
resource/schema → controller → montagem do contexto → engine/regras/política
                     ↓                                   ↓
              repositories/models ← persistência da análise
                     ↓
                  PostgreSQL
```

O motor recebe um contexto pronto e não depende de HTTP/ORM. O controller cuida da transação; repositories não fazem commit. O DTO mantém os três campos de topo exigidos. Lista explícita de regras e configuração versionada são suficientes.

O PDF exige partir do **Base Service**. A implementação bancária da primeira fase é referência de organização, não motivo para copiar todo o domínio de contas e operações. Há uma cópia local de referência em `qi-tech-projeto/referencias/repositorios/bootcamp-base-api`; o link/commit exato indicado para este hackathon não está no PDF e deve ser confirmado antes de iniciar o repositório definitivo. Não instalar outro framework ou alterar a base sem necessidade.

## 10. O que priorizar na avaliação

O PDF apresenta seis critérios, sem pesos: profundidade antifraude, padrão de código, modelagem, solução funcional, documentação e apresentação. Isso favorece uma entrega pequena, coerente e demonstrável.

| Frente | Evidência recomendada |
| --- | --- |
| Profundidade | Combinações justificadas, pouco histórico, contraparte, fracionamento e falsos positivos |
| Código | Camadas da base preservadas, regras independentes, política explícita |
| Dados | Identidades corretas, índices, histórico por status e idempotência |
| Funcionamento | POST persiste antes da resposta; GET recupera exatamente a análise |
| README | Regra, lógica, parâmetros e motivo; precedência e comportamento de 2FA; instruções executáveis |
| Apresentação | Demonstrar os três status e explicar casos de fronteira em sete minutos |

Não entregar um README que descreve regras diferentes das implementadas. Dizer “score 70 reprova” não basta: explicar de onde vem esse valor, quais famílias contribuem, que cenários o cruzam e como evitar rejeitar um cliente legítimo.

### Testes revisados

1. Payload do exemplo oficial aceito e persistido; centavos e fuso interpretados corretamente.
2. Os três status têm motivo em metadata; nenhum quarto status de negócio no sucesso.
3. GET pelo ID do Pix retorna a mesma key, status e metadata após reiniciar o serviço.
4. Pix recusado e com 2FA também são consultáveis.
5. Geolocalização ausente é aceita e não penaliza automaticamente.
6. Primeiro Pix pequeno pode ser aprovado; alto valor sem perfil pode pedir 2FA.
7. Cliente antigo sem histórico local não é tratado como conta recém-aberta.
8. Frequência e montante incluem a intenção atual uma vez, com fronteiras temporais testadas.
9. Retry não aumenta contadores; conteúdo conflitante não sobrescreve a análise.
10. Requisições concorrentes não ignoram o histórico umas das outras no agregado protegido.
11. Dados fora de ordem, mesmo timestamp e offsets diferentes têm comportamento definido.
12. Mudança de IP isolada, conta recebedora recente isolada e pagamento legítimo para novo destino não provocam rejeição automática.
13. PF/PJ, zeros à esquerda, normalização dos documentos e distinção de contas de bancos diferentes.
14. Mudança futura de configuração não altera o GET de uma análise antiga.

## 11. Poucas lacunas restantes

| Lacuna | Assunção recomendada enquanto não houver orientação |
| --- | --- |
| URL/commit do Base Service | Usar a base oficial indicada pela organização; não escolher silenciosamente a implementação da fase anterior |
| Repetição e conflito de `id` | Replay idêntico recupera resultado; conteúdo diferente gera conflito |
| `{id}` do GET | ID externo do Pix, distinto de `key` |
| Autenticação e formato de erro | Preservar o padrão do Base Service; não adicionar novos campos obrigatórios ao payload |
| Eventos atrasados/futuros e latência exigida | Janelas por tempo do evento, recebimento auditado; nenhuma tolerância rígida inventada sem documentar |
| “Tudo que chegou” em corpos inválidos | Registro de recebimentos separado da transação válida, sem poluir o perfil |
| Obrigatoriedade, nulls e tamanhos dos campos | Geolocalização explicitamente opcional; demais presentes no contrato como obrigatórios por hipótese a documentar |

Essas lacunas não exigem travar o desenho das regras. Elas precisam virar decisões explícitas no README ou esclarecimentos pontuais da organização. A entrega do link ao Slack é responsabilidade do grupo; este trabalho apenas analisou a exigência, sem enviar mensagens ou publicar arquivos.
