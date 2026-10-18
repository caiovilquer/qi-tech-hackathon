# Revisão técnica da avaliação v2 e dos anexos

> **Registro histórico:** os achados abaixo se referem aos arquivos recebidos, preservados em [revisao-v2/originais](revisao-v2/originais). A proposta corrigida e vigente está no [Guia de implementação V3](GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md). Os arquivos Python e o schema na raiz foram revisados depois deste diagnóstico; as reproduções antigas agora usam as cópias originais.

Revisão em 17/10/2026, confrontando os quatro arquivos recebidos com o enunciado e as decisões registradas em [ANALISE-ANTIFRAUDE-PIX.md](ANALISE-ANTIFRAUDE-PIX.md). A avaliação de outra LLM foi tratada como proposta a verificar, não como nova instrução do grupo.

> **Conclusão:** aproveitar a organização na Base API, a separação por famílias, os motivos explicáveis e os cenários como ponto de partida. **Não usar ainda a simulação como oráculo da implementação nem declarar a política calibrada.** A demo executa, mas há falhas reproduzíveis e mudanças de produto que contradizem decisões anteriores.

## 1. O que precisa de atenção primeiro

| Prioridade | Achado | Consequência | Correção recomendada |
| --- | --- | --- | --- |
| P1 | Aprovação e 2FA bem-sucedido são tratados como pagamento efetivado | O perfil aprende operações que podem não ter sido executadas | Separar decisão, autenticação e confirmação financeira |
| P1 | IDs distintos são deduplicados por valor e destinatário | Dez Pix distintos de R$50 em dez segundos passam pela regra de frequência | Deduplicar por ID; semelhança pode gerar alerta separado |
| P1 | Um aparelho passa a ser familiar após tentativa com 2FA falho | A segunda tentativa de R$250 é aprovada sem a proteção aplicada à primeira | Familiaridade não equivale a confiança ou cadastro |
| P1 | Simulação usa histórico ilimitado; proposta SQL usa 90 dias | O mesmo aluguel muda de 2FA, score 40, para aprovado, score 15 | Alinhar a cobertura e os critérios de maturidade |
| P1 | Consultas externas vêm antes da idempotência no pseudocódigo | Replay depende novamente dos provedores e pode falhar ou atrasar | Consultar análise persistida antes do enriquecimento; revalidar sob concorrência |
| P2 | Régua por favorecido depende indevidamente do perfil geral | Dois pagamentos antigos de R$100 não acionam a regra em um novo de R$1.000 | Implementar a precedência descrita na seção F, ou corrigir a especificação |
| P2 | Mock não representa fielmente o DICT | Janelas, estado de restrição e atualidade das informações divergem | Contrato de evidências explícito; simulações declaradas |
| P2 | Teste de folha usa vinte documentos de doze dígitos | O cenário não pode ser transportado diretamente para a API com validação de DV | Gerar documentos válidos e passar pelo contrato |

P1 indica algo a resolver antes de adotar a proposta; P2 indica divergência funcional, de evidência ou de integração. Isso não é uma avaliação de um serviço em produção: ainda estamos revisando uma proposta e uma simulação.

**Decisões preservadas:** remetente consultado no primeiro Pix observado por conta; destinatário conforme coeficiente preliminar; motor recebe contexto pronto; três status públicos; análise original permanece imutável após 2FA. Os números de pontuação da v2 continuam candidatos, não decisões aprovadas nesta revisão.

## 2. O que foi efetivamente verificado

- Leitura da avaliação inteira, da simulação, do JSON Schema e dos arquivos relevantes do ZIP: rotas, middlewares, controllers, repositories, erros, schema handler, connector e configuração de banco.
- Execução completa da simulação recebida, com saída salva em [saida-simulacao-recebida.txt](revisao-v2/saida-simulacao-recebida.txt).
- **16 observações reproduzidas:** nove problemas na simulação, seis confirmações técnicas e uma restrição de contrato não oficial. Evidências em [resultados.json](revisao-v2/resultados.json).
- Validação do exemplo oficial com o **SchemaHandler extraído do ZIP e jsonschema 4.17.3**, versão fixada na base; apenas os imports de configuração e erro foram isolados para não inicializar a aplicação.
- Conferência de normas e contratos técnicos em fontes primárias, vinculadas adiante.

**Não foram executados nesta revisão:** API Pix por HTTP, suíte completa da Base API, transações concorrentes em PostgreSQL, integração real com DICT e medição de eficácia antifraude. Os anexos não incluem uma implementação da API Pix nem os testes de concorrência que a outra LLM afirma ter rodado. A tabela de PostgreSQL e os testes de hash relatados na v2 permanecem relatos sem artefatos de execução fornecidos; não são resultados reproduzidos aqui.

O ZIP contém 85 entradas, sem repositório Git. SHA-256:

```text
764a8bd991df9bd1a1e879733a17af886682abbb0d9ab97384de8b08adfc7216
```

Os quatro arquivos recebidos foram preservados. Esta revisão e seus diagnósticos são entregues separadamente para permitir comparação, sem transformar correções conceituais em uma implementação improvisada.

## 3. Correções de semântica e de regras

### 3.1 Aprovação, autenticação e execução são fatos diferentes

Em `politica_v2_simulacao.py:74`, `effective()` retorna verdadeiro tanto para `approved` quanto para `two_factor_required` com `tfa="passed"`. A seção F e a definição de pagamento efetivado na v2 repetem essa equivalência. Ela contradiz a separação que o próprio documento afirma preservar.

O banco pode concluir o 2FA e depois não executar o pagamento: saldo indisponível, limite operacional, cancelamento ou falha de processamento são exemplos. A aprovação do motor também não confirma execução. Sem confirmação financeira, ambos continuam sendo **intenções analisadas**, com estados de autenticação separados.

Modelo recomendado:

| Fato | Exemplo | Uso |
| --- | --- | --- |
| Análise imutável | `two_factor_required` | Resposta original do POST/GET e auditoria |
| Autenticação | Desafio do Pix A concluído, com instante e escopo | Satisfazer o desafio; confiança contextual somente quando explicitamente concedida |
| Execução | Banco informa Pix A executado | Histórico financeiro, sem afirmar que o pagamento foi legítimo |

Para a demo, o banco simulado pode emitir autenticação e execução como **dois eventos explícitos**. Para um MVP menor, pode fornecer um histórico bancário já confirmado. Não é necessário construir um terceiro endpoint público só para obter essa informação.

Se houver webhook, ele é uma extensão interna proposta, não exigência do desafio. Deve identificar evento e desafio, distinguir falha de uma tentativa de falha terminal, deduplicar notificações e guardar instante do fato e do recebimento. Um `passed` não muda a análise original nem cria automaticamente confiança de sessão para Pix futuros. Também não basta comparar `confirmed_at` com a transação: o evento precisa ter sido conhecido antes da decisão que o utiliza.

### 3.2 Mesmo valor e favorecido não identificam um retry

Em `politica_v2_simulacao.py:157`, a frequência ignora operações de mesmo valor e favorecido em até dois minutos, mesmo com IDs distintos. Reproduzido: **dez IDs diferentes, R$50 cada, mesmo destino, em dez segundos → aprovado, sem `VELOCITY_BURST`**.

Isso abre uma forma de contornar a contagem. O contrato já tem uma chave de idempotência: `id`. Só a repetição do mesmo ID e conteúdo representa replay. Toque duplo com outro ID pode gerar `possible_duplicate`, mas não deve desaparecer das contagens de intenções e exposição sem uma correlação confiável fornecida pelo banco.

A motivação de evitar uma espiral de recusas é válida. Resolva-a com resposta idempotente, motivos claros e política específica de repetição, preservando as tentativas. “Dez Pix em dez minutos não têm explicação humana” também é uma afirmação excessiva: é um limiar candidato a testar, não um fato universal.

### 3.3 Tentativa falha não torna aparelho confiável

Em `politica_v2_simulacao.py:234`, `devices_seen` inclui qualquer tentativa anterior. Depois de uma operação em aparelho novo receber 2FA e falhar, a próxima operação no mesmo aparelho deixa de acionar `DEVICE_CHANGE`.

Reprodução: histórico em aparelho antigo → R$250 em aparelho novo exige 2FA → registrar `failed` → novo Pix de R$250 no aparelho novo é aprovado. A tentativa recusada também é suficiente para tornar o dispositivo “visto”.

Manter três dimensões: **observado**, **autorizado pelo banco** e **revogado**. A frequência de observação não altera as duas últimas. A avaliação deve usar o estado atual aplicável à conta/dispositivo e a confirmação contextual pertinente. A regra da v2 também não cobre revogação e deixa de se aplicar na primeira solicitação sem histórico.

R$200/R$1.000 são referências para dispositivos **não cadastrados**, com exceções específicas, e não para qualquer aparelho novo na base antifraude. A soma diária normativa não é uma janela móvel genérica de 24 horas. Usar esses valores como política própria exige nome e justificativa distintos; não demonstra implementação da IN 491. [IN BCB 491, arts. 8–9](https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?numero=491&tipo=Instru%C3%A7%C3%A3o+Normativa+BCB).

### 3.4 A demonstração do aluguel depende de um histórico que a API proposta não leria

O gerador prepara um perfil cerca de 200 dias antes da data-base e avalia aluguéis meses depois. A simulação conserva toda a lista; a seção 3.6 manda consultar apenas 90 dias.

No caso “aluguel sem webhook”, o primeiro aluguel do cenário tem **2FA e score 40** com a lista completa. Recortando exatamente o histórico proposto para o repository, o resultado passa a **aprovado e score 15**. Isso invalida usar a tabela como expectativa de integração sem ajustar a cobertura.

Além disso, a seção F diz que dois pagamentos ao mesmo favorecido bastam para formar a régua específica. O código só entra em `AMOUNT_ABOVE_PROFILE` quando existe perfil geral de cinco pagamentos em três dias. Dois registros antigos de R$100 para um destino, seguidos por R$1.000, produzem **score zero**, embora a regra descrita previsse o desvio de 10×.

Alinhar a janela do histórico em um único parâmetro, separar maturidade geral e maturidade por favorecido e usar eventos de execução confirmados. A régua baseada no máximo também precisa de avaliação: um pagamento excepcional pode elevar o patamar de todas as análises posteriores. O documento geral recomenda referência robusta; trocar por máximo é uma escolha de política, não uma correção técnica já aceita.

### 3.5 O mock do DICT é uma aproximação, não a fonte real

Problemas identificados:

- `m12` usa 365 dias móveis; no contrato documentado, considera meses completos sem o mês corrente. Uma marcação de ontem aparece indevidamente nesse contador. `d90` também precisa seguir os limites de calendário da fonte.
- Marcações não têm cancelamento, notificações abertas nunca fecham e não há watermark/validade no retorno.
- `COUNTERPARTY_RESTRICTED` não recebe uma restrição explícita, embora a tabela da v2 diga que esse caso está coberto; o código usa somente contadores.
- O total de notificadores distintos não demonstra que cada um confirmou a mesma categoria ou a mesma conta. Uma regra de bloqueio baseada nesses agregados seria política própria; não uma restrição oficial retornada pela fonte.
- `evaluate()` chama `dict_stats()` diretamente e recebe `Mocks`. Isso acopla a simulação ao provedor; na implementação, o motor deve receber evidências normalizadas já coletadas.
- Só o destinatário é consultado. A simulação não sustenta o exemplo de metadata que declara `dict_payer="ok"` nem a afirmação de consulta dos dois lados em cada Pix.

As diferenças de períodos e estrutura podem ser conferidas na [API oficial do DICT](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/API-DICT.html). A API de estatísticas não oferece nesta operação uma consulta histórica arbitrária por `transaction_date`: o histórico temporal do mock é uma capacidade de simulação e precisa ser apresentado assim.

É possível criar um universo sintético no qual documentos sem fixture específica tenham contadores zero. Isso precisa ser **uma premissa do simulador**, com cobertura declarada. Não equivale a provar que qualquer ausência de informação externa é resposta favorável. Distinguir resposta válida sem ocorrências, documento fora da cobertura, fonte indisponível e sinal desatualizado; nenhum desses casos deve forçar 2FA indiscriminadamente.

### 3.6 Valores e dados dos cenários

`Run.send()` faz `int(reais * 100)`. Reproduzido: R$0,29 vira 28 centavos e R$1,15 vira 114 centavos. Receber centavos inteiros desde o gerador, ou usar conversão exata a partir de texto, elimina a perda.

Os vinte favorecidos da folha são gerados com doze dígitos. Os exemplos de fundo também não garantem DV válido. A simulação os aceita porque não passa pelo schema/controller. Portanto, o resultado “folha aprovada” não demonstra compatibilidade desses payloads com a API proposta.

O cenário com muitos pagadores mede solicitações observadas, inclusive recusadas, e não recebimentos liquidados. Esse agregado pode ser útil, mas não deve ser chamado de dinheiro recebido. Para `PASS_THROUGH`, é necessário histórico de entradas executadas; não inferi-lo apenas das solicitações de outros pagadores que passaram pelo nosso serviço.

## 4. Mudanças de produto que a v2 não pode aprovar pelo grupo

| Tema | Decisão atual / avaliação desta revisão |
| --- | --- |
| Destinatário consultado sempre | A v2 propõe trocar o gatilho por coeficiente. A limitação da consulta seletiva é real e já está documentada, mas a decisão aprovada continua sendo condicional |
| Remetente consultado em todo Pix | A v2 propõe abandonar a consulta inicial. É uma alternativa que melhora cobertura de mudanças futuras, com custo próprio; não substitui silenciosamente a escolha do grupo |
| Dispensa de chamadas por `IncludeStatistics` | Reutilizar resposta prévia do banco é útil. O payload do desafio não contém chave nem essa resposta; portanto, não garante que as estatísticas já estejam disponíveis sem enriquecimento |
| Webhook público de 2FA | É uma opção de integração interna, não uma obrigação herdada do webhook de exemplo da base |
| Limiares 30/70, teto noturno R$3.000, fator PJ ×5 | Parâmetros candidatos sem calibração empírica ou aprovação explícita do grupo |
| Todos os CNPJs fora de frequência | Folha legítima exige contexto, mas PJ também pode ser comprometida. Criar política própria para seu perfil, sem imunidade automática por tipo documental |
| Transferência ao mesmo titular com metade dos pontos | Pode ser uma hipótese a testar; não neutraliza conta recém-aberta, tomada de conta ou outras evidências independentes |
| Favorecido habitual por CPF/CNPJ | Definir se o vínculo é com a pessoa ou com a conta. Um pagamento antigo a um CPF não cadastra automaticamente todas as contas atuais dele como confiáveis |

As duas políticas de DICT aprovadas anteriormente permanecem no documento geral. Os limites dessa escolha não foram escondidos: não consultar o remetente novamente reduz a capacidade de detectar marcações posteriores; confiança alta no pagador não comprova segurança do destinatário.

## 5. Ajustes regulatórios na avaliação

**R$1.000 à noite não é simplesmente um piso de 2FA do Bacen.** A Resolução 142 trata limite operacional, mecanismos de ajuste e contas cadastradas. A redação consultada mantém a exceção para empresários individuais. Um pagamento anterior ao favorecido há 24 horas não equivale ao cadastro bancário de conta autorizada a receber acima do limite. [Resolução BCB 142, arts. 2–3](https://www.bcb.gov.br/estabilidadefinanceira/exibenormativo?numero=142&tipo=Resolu%C3%A7%C3%A3o+BCB).

É possível propor uma regra antifraude própria que peça 2FA a partir de R$1.000 e recuse acima de R$3.000, mas isso é separado do limite efetivo validado pelo banco. A frase “esperar as 6h, agendar ou aumentar o limite” não define uma exceção implementada: a regra candidata continua recusando acima de R$3.000 à noite mesmo se um limite bancário maior tiver sido aprovado. Agendar também não deve contornar a política aplicável no momento correto da operação.

**Tempo adicional de autorização não é retenção pelo recebedor por 30/60 minutos.** O Manual de Tempos atribui esses prazos, nos casos previstos, à autorização de iniciação pelo PSP do pagador. Bloqueio cautelar no recebedor é outro mecanismo. Evitar fundamentar a apresentação exclusivamente em comunicado de 2021 ou afirmar, sem avaliação, que reter por alguns minutos é “a ferramenta mais forte”. [Manual de Tempos do Pix, seção 2](https://www.bcb.gov.br/content/estabilidadefinanceira/pix/Regulamento_Pix/IX_ManualdeTemposdoPix.pdf).

**CNPJ alfanumérico:** a recomendação de suportar letras é pertinente à data do hackathon. A Receita publicou a emissão do primeiro registro em 31/07/2026; os formatos coexistem. Os exemplos do algoritmo de DV da v2 foram reproduzidos. Validade do DV não prova existência cadastral, titularidade nem regularidade do documento. [Receita Federal](https://www.gov.br/receitafederal/pt-br/assuntos/noticias/2026/julho/receita-federal-gera-o-primeiro-cnpj-em-formato-alfanumerico).

## 6. Base API, contrato e persistência

### O que está correto e pode ser aproveitado

| Aspecto | Conferência no ZIP |
| --- | --- |
| Framework e camadas | FastAPI, JSON Schema, controllers, repositories/models, DTOs e SQLAlchemy |
| Validação | `SchemaHandler` valida antes do resource; validações semânticas ficam no controller |
| Erros | Envelope QIT; códigos duplicados são verificados no início |
| Banco | Controller faz commit; middleware encerra a sessão e trata rollback; `autoflush=False` |
| Token e rastreio | `INTERNAL-TOKEN`, exceções públicas e `x-request-id`; documentação automática desabilitada |
| Connector | `RestConnector` recebe timeout; precisa de adaptação para disponibilidade, contrato e unidade de tempo |
| Datas financeiras | `TIMESTAMPTZ`/`DateTime(timezone=True)` são adequados para os novos campos do Pix; preservar entrada original |

O webhook de incremento da entidade de exemplo não é um subsistema pronto de resultado de autenticação. O connector também não oferece automaticamente política de falhas, cache ou deadline global: `requests` usa timeout em segundos, enquanto a proposta de configuração se chama `DICT_TIMEOUT_MS`. Fazer a conversão e tratar falhas de conexão, timeout e respostas inválidas explicitamente.

### JSON Schema: utilizável, mas não é a validação inteira

Confirmado com a versão da base:

- O exemplo oficial passa; `150000.0` também passa como inteiro matemático e pode ser normalizado após validação. `true` é rejeitado.
- O resolver com referenciador `None` falha em `$ref` interno; o schema expandido recebido contorna isso.
- Regex de timestamp não verifica calendário; o schema também deixa IP inválido passar. Validar data real e offset; aplicar uma política explícita ao IP antes da coluna `INET`.
- `geolocation` opcional e `null` são aceitos. `(0,0)` é uma coordenada possível, não um marcador universal de ausência.
- `[A-Za-z0-9_-]{1,64}`, teto monetário, tamanhos e rejeição de campos extras são escolhas adicionais. A regra de `x-request-id` não define o contrato do ID do Pix. Documentar essas restrições antes de chamá-las de oficiais.

O schema pode servir de ponto de partida. A afirmação “pronto e validado” deve ser entendida como compatibilidade sintática verificada, não implementação completa de DV, calendário, coerência entre campos ou persistência.

### Ordem recomendada do processamento

1. Validar e normalizar; calcular identidade e hash canônico sem incluir dados mutáveis dos provedores.
2. Procurar análise existente pelo ID. Replay retorna a resposta salva; conflito retorna erro antes de consultar fontes.
3. Para nova intenção, montar o contexto e aplicar os gatilhos de enriquecimento aprovados. Encerrar a transação de leitura quando necessário para não reter conexão enquanto aguarda provedores.
4. Sob a coordenação necessária, revalidar ID e histórico. Aplicar a regra de primeiro Pix por conta e reavaliar o gatilho do destinatário conforme o documento geral.
5. Executar o motor puro e persistir Pix, análise e snapshot atomicamente. Responder somente após confirmação do commit.

O pseudocódigo da v2 precisa ter tratamento de erro abrangendo também eventual `flush`, não apenas `commit`, e distinguir a violação da constraint de ID de outros `IntegrityError`. Nem todo erro de integridade é replay; se a releitura não encontra a transação esperada, não há resultado idempotente para devolver. O SQLAlchemy pode emitir escritas no flush, inclusive explicitamente antes do commit. [Documentação SQLAlchemy](https://docs.sqlalchemy.org/en/20/orm/session_basics.html#flushing).

`READ COMMITTED` com serialização por pagador é uma direção coerente para as contagens. Incluir o namespace do integrador na identidade e no lock. A Base API não resolve essa concorrência por si só. Dispensar coordenação no destinatário é um trade-off: subcontagem concorrente pode mudar o score e a ação. Se houver limite rígido por destinatário, é necessário garantir sua consistência; se for sinal aproximado, declarar a possibilidade de subcontagem.

Os códigos `QIT001001` a `QIT001007` já existem no ZIP. Não adicionar novas classes com os mesmos códigos sem remover/substituir deliberadamente as antigas. Não afirmar que o código proposto pode ser simplesmente colado ao lado do exemplo.

### Dados e testes

Manter a relação 1:1 entre solicitação e análise é adequado. Completar o desenho com estado persistente da verificação inicial por conta, evidências de autenticação e, quando utilizadas, de execução. O banco de risco não precisa copiar saldo, ledger ou cadastro bancário.

O snapshot deve registrar cobertura, indisponibilidade, origem simulada, instante em que a evidência era conhecida e versão da política. Evidência histórica não deve esconder restrição de segurança atualmente vigente em uma autorização ao vivo; `transaction_date` antigo não pode ser um atalho para ignorar uma revogação atual.

Preservar timestamps de auditoria com fuso. `raw_payload` conserva a representação recebida; `TIMESTAMPTZ` conserva o instante, não o texto do fuso original. A soma de intenções em 2FA pendente pode ser uma política conservadora de exposição, mas não é saldo reservado nem dinheiro efetivamente gasto.

`docker compose down -v` apaga os volumes do projeto. Não recomendar isso como procedimento padrão de evolução do schema. Para demo descartável, documentar a perda dos dados; para desenvolvimento com histórico necessário, usar migração ou outro banco/volume isolado. Nenhum volume foi removido nesta revisão.

## 7. Como aproveitar a v2 sem carregar seus erros

**Aproveitar agora:** organização da base, schema como ponto de partida, política versionada, resultados explicáveis por família, dinheiro em centavos, datas com fuso, análise persistida, testes de contrato/idempotência/concorrência e cenários legítimos como aluguel, comerciante e folha.

**Corrigir antes da implementação:** separar execução de 2FA, contar IDs distintos, manter confiança do aparelho independente de tentativas, alinhar janela e maturidade de perfil, manter provedores fora do motor, antecipar a idempotência e modelar disponibilidade dos sinais.

**Manter como propostas:** pontuações e cortes da v2, limite próprio de coação, exceções para PJ/mesma titularidade, consulta universal ao DICT e webhook público. Construir uma tabela de cenários desejados com justificativa e depois ajustar parâmetros; não usar a saída do próprio algoritmo como única prova de que o algoritmo está correto.

O cronograma por horas da outra LLM é sugestão circunstancial. A revisão não congelou essas decisões nem iniciou a implementação da API.

## 8. Reprodução

O verificador usa biblioteca padrão e `jsonschema==4.17.3`; não conecta a banco e não inicia a aplicação. Na máquina desta revisão, foi utilizado um ambiente isolado em `tmp/revisao-v2/.venv`:

```bash
tmp/revisao-v2/.venv/bin/python revisao-v2/verificar.py
```

Para outra máquina, criar um ambiente virtual e instalar `jsonschema==4.17.3`, depois executar [verificar.py](revisao-v2/verificar.py). O arquivo de resultados inclui hashes dos quatro materiais recebidos. **Execução bem-sucedida confirma a reprodução dos achados, não a correção da política.** As inconsistências continuam presentes nos anexos originais preservados para comparação.
