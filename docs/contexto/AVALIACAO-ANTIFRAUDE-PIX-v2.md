# Avaliação da proposta — Antifraude Pix (v2)

> **Proposta recebida, superada pela revisão:** este texto da outra LLM foi preservado como referência, mas não é o guia vigente. Usar o [Guia de implementação V3](GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md) e os arquivos Python corrigidos na raiz. A cópia integral original, sem este aviso, está em [revisao-v2/originais](revisao-v2/originais). Alegações abaixo sobre calibração, conformidade ou execução de testes pertencem ao texto recebido e não comprovam a implementação local.

Segunda rodada da avaliação. Ela responde às perguntas do time e refaz a análise sobre o Base Service (`bootcamp-base-api`), que será o esqueleto do projeto. Escrita em 17/10/2026, às 22h30. Entrega do repositório: 18/10, às 12h.

> **Veredito v2.** A base já resolve boa parte do que era risco de engenharia: validação em duas camadas, formato de erro, sessão e commit, log com `x-request-id`, webhook e connectors com timeout. O que sobra é o motor e três decisões de produto, todas fechadas aqui:
> - o mock se comporta como a fonte real;
> - há dois limites noturnos;
> - o resultado do 2FA volta por webhook.
>
> Com isso, a política fica calibrada e testável por HTTP. Os cenários da seção 4.3 foram rodados, não calculados de cabeça.

**Anexos:**
- `politica_v2_simulacao.py`: a política v2 em Python puro, sem dependências. Gera a tabela de calibração e serve de oráculo para os testes.
- `post_pix_transaction.json`: o schema de entrada, pronto para `src/schemas/` e validado com o resolver da base.

**Ondas de implementação**, usadas nas seções abaixo:
- **onda 1**: núcleo, sem mocks;
- **onda 2**: completa a história da demo (mocks, webhook, regras de apoio);
- **onda 3**: só se sobrar tempo.

## 1. O que mudou desde a v1

| Tema | v1 | v2 | Por quê |
|---|---|---|---|
| CPF desconhecido no mock | estatísticas zeradas | mantido, como regra explícita do mock | é o que a fonte real responde (A) |
| Consulta de quem recebe | gatilho explícito no lugar do `C_pre` | sempre, com timeout | a estatística chega junto da consulta da chave (B) |
| Consulta do pagador | validade de 24h | a cada Pix no mock; em produção, cache e evento | conta-mula (C) |
| Limite noturno | piso de 2FA acima de R$ 1.000 | piso de 2FA mais teto de R$ 3.000 que reprova | coação (D, E) |
| Resultado do 2FA | "não reenviar", no README | webhook no padrão da base | o motor aprende o aluguel (F) |
| Janela do perfil | dias anteriores ao atual | pagamentos com mais de 24h | o Pix das 23h50 inflava o perfil às 0h10 (F) |
| `VELOCITY_BURST` | ≥ 5 em 10 min: +20 | +30, pede 2FA sozinho | 5 Pix em 10 min já merecem confirmação (G) |
| `DAILY_AMOUNT` | qualquer soma | só com ≥ 2 Pix na janela | um Pix grande já pontua em Perfil; evita contar duas vezes |
| `STRUCTURING` | qualquer limiar da política | só R$ 1.000, R$ 3.000 e R$ 5.000 | a faixa de R$ 170 a R$ 200 é comum demais |
| `NEW_PAYEE_FANOUT` | todo pagador | só cliente com perfil | "novo" só vale com histórico (v1 §2.6) |
| DV inválido | aceitar com alerta | 422, como a base | o documento chega validado (H) |
| Origem e destino na mesma conta | 422 | aceitar com alerta | decisão do time (J) |
| Formato de erro | `problem+json` | o da base (`QIT...`) | alinhamento com a base |
| Agregados | uma consulta SQL com `FILTER` | histórico de 90 dias do pagador e uma consulta do destino; janelas calculadas no motor | regra num lugar só, testável sem banco, igual à simulação |

## 2. Respostas às perguntas do time

### A. Usuários novos: explicar o 2FA ou mudar o mock? (v1 §2.1)

A intuição sobre a realidade está certa: em produção quase sempre há dado. Por isso mesmo, o mock precisa se comportar como a fonte real, e não como "não sei".

- **O pagador é cliente do próprio banco.** Data de abertura, aparelho do onboarding e histórico sempre existem.
- **O DICT sempre responde sobre quem recebe por uma chave cadastrada.** Para quem não tem ocorrência, a resposta são contadores zerados com marca d'água, não um vazio ([API do DICT][dict]).
- **"Não sei" só aparece quando a fonte falha.** Aí a prática é modo degradado: valor baixo segue, valor alto pede confirmação, e nada é bloqueado em massa.

No mock, então, documento desconhecido devolve estatísticas zeradas, com estado `ok`, e vale 0 ponto. Os fixtures servem para injetar evidência nos cenários da demo (marcação de mula, aparelho autorizado, recuperação de conta), nunca como condição para aprovar.

Explicar na apresentação não resolve:
- a banca corrige antes da apresentação, lendo o README e rodando o projeto;
- o critério 4 pede que "as decisões batem com as regras descritas", e "CPF fora do mock pede 2FA" não seria uma regra de verdade;
- um motor que só aprova CPF pré-cadastrado parece calibrado para a demo, e contradiz o slide do aluguel.

A parte certa da ideia, cautela com quem o motor não conhece, já está em `LOW_HISTORY_EXPOSURE`, proporcional ao valor. Na simulação:
- CPF desconhecido, R$ 50 às 14h → aprovado;
- exemplo oficial → 2FA pelo limite noturno;
- o mesmo payload às 14h → aprovado.

### B. O que entra no lugar do `C_pre` (v1 §2.2)

Tirem o coeficiente e a decisão de consultar ou não quem recebe. Consultem sempre, com timeout curto.

1. **Na vida real, não há o que economizar.** Todo Pix por chave para outro banco consulta o vínculo no DICT antes de sair (ou reaproveita a resposta em cache), e essa consulta aceita `IncludeStatistics`: a mesma resposta traz os contadores antifraude do dono e da chave ([API do DICT][dict]). É a consulta que acontece quando o cliente digita a chave, o momento em que vocês viram o Nubank avisar.
2. **Confiança alta no pagador é o retrato do golpe.** Na engenharia social, o titular verdadeiro, no próprio aparelho, paga um golpista. O coeficiente pularia a checagem justamente aí.
3. **Menos código.** Somem a segunda escala ("confiança"), o corte `T_dict` e o laço de revalidação sob lock da §11 da análise.

O que fica no lugar é só política sobre a resposta:

| Resposta do mock do DICT sobre quem recebe | Efeito |
|---|---|
| Restrição por fundada suspeita, ou mula/golpista em 90 dias com ≥ 2 notificadores distintos | reprova (`COUNTERPARTY_RESTRICTED`) |
| Notificação aberta, marcação de um notificador só, ou marcação nos últimos 12 meses | +30 e alerta (`COUNTERPARTY_REPORTED`) |
| Tudo zerado, inclusive documento desconhecido | 0 |
| Timeout ou erro | +10 (`COUNTERPARTY_CHECK_UNAVAILABLE`) |

O payload não traz a chave usada. Por isso, o mock representa essa resposta pelo documento de quem recebe, no formato de `getPersonStatistics`. Escrevam isso no README.

### C. Consultar o pagador só no primeiro Pix (v1 §2.3)

**O problema.** Uma conta-mula vive poucas horas ou dias:
- abre, ou é alugada;
- recebe das vítimas;
- repassa em minutos;
- é abandonada.

As marcações no DICT só nascem depois, quando as vítimas reclamam e os bancos delas registram notificação de infração e marcação de fraude.

```text
09:00        a conta X recebe o 1º Pix de uma vítima    ← consulta "só no 1º Pix": X está limpa
09:00–11:00  recebe de mais cinco vítimas e repassa
12:00        os bancos das vítimas marcam X como mula
13:00        X tenta repassar R$ 3.000                    ← nada nessa política vê a marcação
```

A consulta única olha X quando ela ainda era nova, e conta nova quase sempre está limpa. X aparece como pagadora porque o dinheiro das vítimas chega nela e ela é cliente de algum banco; neste cenário, o nosso, que analisa o repasse.

**A solução comum** é tratar o dado de fora como cache com prazo, não como fato permanente:

1. **Evento primeiro.** O banco de X recebe as notificações de infração dos Pix que X recebeu (no DICT, por polling da lista de notificações). Ao receber, marca o próprio cliente, e todo Pix seguinte vê a marca sem chamada externa.
2. **Prazo curto para o que vem de fora.** As estatísticas do pagador ficam num snapshot com validade, por exemplo de 24h.
3. **Renovação forçada quando o risco pede:** Pix acima de R$ 1.000, favorecido novo ou padrão de conta de passagem.
4. **Se a renovação falhar,** o motor usa o último snapshot e registra que ele está velho. Nunca o trata como "limpo".

Duas referências para a apresentação:
- a consulta de estatísticas de pessoa tem limite de 12.000 fichas por minuto por participante ([API do DICT][dict]). Sobra para um PSP médio, mas um banco grande não consultaria a cada Pix, e é por isso que existe o cache;
- as regras de novembro de 2024 exigem conferir as marcações dos próprios clientes pelo menos a cada seis meses ([Tecnoblog][tecnoblog-2024]). É o piso, não a frequência ideal. A §4.1 da análise já cita o art. 89.

**No hackathon:**
- **onda 2:** consultar o mock a cada Pix, o que custa zero, e registrar o estado no snapshot da análise. No README, descrever a versão de produção acima;
- **onda 3:** `PAYER_FLAGGED` (pagador marcado → reprova) e `PASS_THROUGH` (recebeu de ≥ 5 pagadores distintos em 24h e está mandando ≥ 80% do que entrou);
- **para demonstrar o ciclo,** o mock tem de ser temporal: cada marcação tem data, e o mock só devolve as criadas até a `transaction_date`. O mesmo fixture mostra X limpa às 11h30 e marcada às 13h, sempre igual.

### D. Como tratar a coação (v1 §2.5)

Na coação (sequestro relâmpago, saidinha de banco, invasão de casa), quem opera é o titular verdadeiro, no aparelho dele, com senha e biometria certas. O coator obriga a vítima a passar no 2FA. **2FA prova identidade, não vontade.**

O arranjo Pix combate a coação com tempo e teto, coisas que a vítima não consegue destravar na hora, nem sob ameaça:
- limite de R$ 1.000 entre pessoas físicas, incluindo MEI, das 20h às 6h. O cliente pode optar por 22h–6h ([comunicado do BCB][bcb-2021]; [Jornal Contábil][jc-noturno]);
- aumento de limite pelo app leva no mínimo 24h e no máximo 48h para valer. Conta cadastrada para receber acima do limite também só vale depois de 24h ([comunicado do BCB][bcb-2021]);
- o PSP de quem recebe pode reter o crédito por 30 minutos de dia ou 60 à noite para análise ([comunicado do BCB][bcb-2021]). Depois, o MED tenta devolver.

O nosso motor tem três status, e nenhum deles é "reter". O que dá para fazer:

1. **Um teto que nenhuma autenticação destrava:** `NIGHT_HARD_LIMIT`, no item E.
2. **A exceção é a relação antiga.** Quem recebeu pagamento efetivado do cliente há pelo menos 24h fica fora do teto, porque o coator quer o dinheiro numa conta dele, que a vítima nunca pagou. É a mesma lógica da conta cadastrada.
3. **Conta própria aberta há menos de 7 dias entra no teto.** É o golpe da "conta segura" e a variação do sequestro em que o criminoso abre uma conta no nome da vítima, transfere para lá e saca de outro aparelho.
4. **O resto vem da combinação.** Favorecidos novos em sequência, valor acima do perfil e acumulação reprovam por score antes do teto.
5. **A metadata nomeia o risco:** `risk_type: "coercion_exposure"` e um `customer_warning` para o banco usar.

Na simulação:
- R$ 5.000 às 23h para favorecido novo com conta antiga (a laranja típica) → reprovado pelo teto. Sem ele, o score daria 40 e o Pix sairia com 2FA;
- quatro Pix de R$ 1.000 às 23h para quatro contas diferentes → aprovado, 2FA, reprovado, reprovado.

Limitação para o README: reter o Pix por alguns minutos é a ferramenta mais forte contra coação e golpe, e o contrato não tem esse status. Fica em próximos passos.

### E. Dois limites noturnos

Sim, é o modelo certo: o primeiro trata identidade, o segundo trata coação.

| Regra | Quando vale | Efeito |
|---|---|---|
| `NIGHT_SOFT_LIMIT` | CPF para CPF de outra pessoa, das 20h às 6h, quando o valor ou o acumulado da noite passa de R$ 1.000 | no mínimo 2FA |
| `NIGHT_HARD_LIMIT` | à noite, para favorecido sem pagamento efetivado há 24h ou mais, ou para conta própria aberta há menos de 7 dias, quando o acumulado da noite para esses destinos passa de R$ 3.000 | reprova; o 2FA não destrava |

**Por que R$ 3.000.** Não há dado rotulado, então o número sai dos cenários. É o teto de perda por noite que aceitamos sob coação, para destino sem relação:
- o exemplo oficial (R$ 1.500) continua em 2FA;
- uma compra de usado à noite, até R$ 3.000, passa com confirmação;
- o Pix de R$ 5.000 da coação é reprovado.

Quem precisar de mais pode esperar as 6h, agendar ou usar o aumento de limite do banco, que leva 24h.

**Cuidados de implementação:**
- o acumulado conta Pix aprovados e em 2FA ainda não recusado. Reprovado não conta, porque o dinheiro não saiu;
- depois da meia-noite, a noite começa às 20h do dia anterior;
- a opção 22h–6h do cliente não está no payload: usem 20h–6h e digam isso no README;
- o comunicado do BCB inclui MEI, mas o payload não distingue o CNPJ de um MEI, então a regra cobre CPF→CPF. A §4.3 da análise fala em "exceto empresários individuais"; vale conferir a redação consolidada da Res. 142;
- reenviar o exemplo oficial com ids novos dá 2FA, 2FA e reprovado (R$ 4.500 na noite). A metadata precisa mostrar o acumulado, e o README precisa avisar.

### F. Perfil por favorecido e o primeiro aluguel (v1 §2.8)

A régua do perfil tem três degraus:

1. **≥ 2 pagamentos efetivados ao mesmo favorecido**, com mais de 24h → a régua é o maior deles.
2. **Senão, se o cliente tem perfil** (≥ 5 pagamentos efetivados em ≥ 3 dias, todos com mais de 24h) → a régua é o maior Pix efetivado dele.
3. **Senão,** não há perfil, e vale só `LOW_HISTORY_EXPOSURE`, pelo valor.

Com a razão `valor / régua`: acima de 2× soma +15; acima de 5×, +30.

**O primeiro aluguel** (R$ 2.500, maior Pix do cliente R$ 400) cai no degrau 2:
- razão 6,25 → +30;
- favorecido novo → +10;
- total 40 → 2FA.

É o comportamento desejado, e o que os bancos fazem: primeira transferência alta para alguém novo pede confirmação.

**O problema é o mês seguinte.** O primeiro aluguel ficou em `two_factor_required`, e o motor não sabe se o cliente confirmou.
- Se 2FA não conta como pagamento, o aluguel pede 2FA todo mês.
- Se conta sempre, um golpista que falhou no 2FA ensina o motor a aceitar a mula.

**A solução é um webhook de resultado**, no mesmo padrão do `PUT /webhook/sample_entity/{key}/increment_counter` da base:

```text
PUT /webhook/pix/transaction/{id}/two_factor
INTERNAL-TOKEN: default_token

{"result": "passed", "confirmed_at": "2026-10-17T22:16:10-03:00"}

→ 204
```

- Com `passed`, o Pix passa a contar como pagamento efetivado. Com `failed`, deixa de contar até no acumulado.
- Só vale para Pix em `two_factor_required` (senão, 409). Repetir o mesmo resultado devolve 204; mudar um resultado já registrado dá 409.
- O motor só usa resultado confirmado antes do Pix que está avaliando: nenhum fato posterior muda uma decisão passada.
- O GET continua devolvendo a análise original, sem mudança. É a separação, que a análise já defendia, entre decisão, resultado do 2FA e execução.

Na simulação:
- com webhook, o 1º mês pede 2FA, os seguintes são aprovados e, a partir do 4º, entra o bônus de recorrência;
- sem webhook, o aluguel pede 2FA todo mês.

**Um furo encontrado na simulação:** com "dias anteriores ao atual", o Pix das 23h50 entrava no perfil às 0h10 e inflava a régua do Pix seguinte. Por isso o perfil passou a usar só pagamentos com mais de 24h.

### G. Velocity

**Velocity é frequência:** quantos Pix numa janela curta, sem olhar o valor. Montante é quanto somou numa janela; Perfil é quanto destoa do habitual. As três se completam.

**Por que importa:** fraude é corrida contra o tempo.
- Depois de tomar a conta, o fraudador esvazia antes de a vítima perceber.
- Robôs disparam dezenas de Pix por minuto.
- Teste de conta usa Pix pequenos em sequência, que escapam de qualquer regra de valor.

**Como calcular:** janela deslizante `(t − Δ, t]` sobre o histórico do pagador, ancorada na `transaction_date` (não no relógio do servidor), contando o Pix atual uma vez.
- Conta tentativa em qualquer status: um reprovado também foi uma tentativa.
- Replay do mesmo `id` não conta.
- Retry, isto é, mesmo favorecido e mesmo valor em até 2 minutos, conta como um. Isso evita a espiral em que o cliente reprovado tenta de novo e se reprova mais.

**As regras:**
- `VELOCITY_BURST`, só para CPF: ≥ 5 Pix em 10 minutos → +30, que já pede 2FA sozinho; ≥ 10 → reprova.
- `NEW_PAYEE_FANOUT`, só para cliente com perfil: ≥ 3 favorecidos novos em 1 hora → +20.

**Por que esses números.** 5 Pix em 10 minutos é raro para pessoa física, mas acontece (dividir uma conta entre amigos), então pede confirmação em vez de bloquear. 10 em 10 minutos não tem explicação humana. CNPJ fica fora da rajada porque folha e fornecedores saem em lote.

**Velocity em outras dimensões:**
- do destino: `MULE_FAN_IN`, muitos pagadores distintos para uma conta nova em 24h;
- do aparelho: `SHARED_DEVICE`, o mesmo aparelho usado por vários pagadores.

Na simulação:
- 10 Pix de R$ 50 em 9 minutos para pessoas diferentes → 4º aprovado, 5º em 2FA, 10º reprovado;
- folha com 20 salários em 10 minutos → todos aprovados.

### H. DV do CPF: a validação já não aconteceu antes?

Aconteceu, e vocês têm razão.
- O documento de quem recebe vem do DICT. A chave foi conferida com a Receita no cadastro, e o DICT tem até um motivo próprio, `RFB_VALIDATION`, para corrigir vínculos ([API do DICT][dict]).
- O documento do pagador vem do cadastro do próprio banco.

Quando o Pix chega ao motor, o documento já é válido. DV errado significa bug ou payload adulterado: é erro de contrato, não sinal de fraude.

Isso inverte a minha recomendação da v1 e alinha com a base, que já faz essa divisão:
- o formato fica no schema (400, `QIT000001`);
- a conta dos dígitos fica no controller, com `is_valid_cpf` (422, `QIT001003`).

Estendam `utils/document_number.py` com o CNPJ alfanumérico abaixo e recusem com 422. O risco que eu apontava, de a banca digitar CPF à mão, se resolve no README: todos os exemplos com CPFs válidos, como já são os do exemplo oficial.

```python
CNPJ_PATTERN = re.compile(r"[0-9A-Z]{12}[0-9]{2}")
CNPJ_WEIGHTS = ((5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))


def normalize_document(raw: str) -> str:
    return re.sub(r"[.\-/]", "", raw).upper()


def is_valid_cnpj(document_number: str) -> bool:
    cnpj = normalize_document(document_number)
    if not CNPJ_PATTERN.fullmatch(cnpj) or len(set(cnpj)) == 1:
        return False
    values = [ord(c) - 48 for c in cnpj]  # '0'..'9' → 0..9, 'A' → 17 ... 'Z' → 42
    for position, weights in zip((12, 13), CNPJ_WEIGHTS):
        remainder = sum(v * w for v, w in zip(values[:position], weights)) % 11
        if values[position] != (0 if remainder < 2 else 11 - remainder):
            return False
    return True
```

Testado: `12.ABC.345/01DE-35` e `11.222.333/0001-81` passam; `11.222.333/0001-82` e `00000000000000`, não.

A observação sobre o Nubank leva a uma distinção útil:
- o aviso na digitação da chave acontece no **momento 1** do Pix, a consulta da chave, antes do valor;
- o nosso motor é o **momento 2**, logo antes de o dinheiro sair.

Um endpoint de pré-checagem do destino seria natural, mas está fora do contrato. Deixem em próximos passos.

### I. `opening_date` divergente para a mesma conta

Cada payload traz a data de abertura das duas contas. A mesma conta de destino (mesmo ISPB, agência e número) aparece em muitos Pix, e nada impede que dois payloads digam datas diferentes:

```text
pix-1   destino 60701190 / 3675 / 10442   opening_date 2026-10-10   → conta de 7 dias
pix-2   destino 60701190 / 3675 / 10442   opening_date 2019-01-01   → conta de 7 anos
```

**Por que importa.** As regras de contraparte usam a idade da conta (`RECENT_PAYEE_ACCOUNT`, `MULE_FAN_IN`). Se a conta mora numa tabela e cada payload sobrescreve a data, a decisão de um Pix passa a depender do payload de outro, e uma análise antiga deixa de ser reproduzível.

Na vida real isso não acontece, porque o dado vem do DICT e do cadastro. É higiene para payloads digitados à mão.

**O que fazer:**
- cada Pix decide com a data do próprio payload, gravada na própria linha do Pix;
- se a conta já apareceu com outra data, a metadata ganha `data_quality: ["OPENING_DATE_MISMATCH"]`, sem pontos;
- se houver tabela de contas, ela guarda a identidade (ISPB, agência, número e titular), não a data.

### J. Pix para conta própria

Concordo, e já está permitido. Mandar para outra conta do mesmo titular, no mesmo banco ou em outro, é comum: investimento, carteira digital, dinheiro de crédito indo para a conta principal. A política trata isso com `SELF_TRANSFER_NEW_ACCOUNT`:
- não conta como favorecido novo;
- Perfil e Montante valem metade;
- conta própria aberta há menos de 7 dias soma +15, com alerta (golpe da "conta segura"), e entra no teto noturno.

O que eu tinha marcado como erro é outra coisa: origem e destino serem **a mesma conta**, com mesmo banco, agência e número. Isso não move dinheiro, e os apps não permitem. Se quiserem aceitar, aceitem com `data_quality: ["SAME_ACCOUNT"]` e sem pontos, o que é inofensivo. Para detectar, configurem o ISPB da instituição (`HOME_ISPB`), porque a origem não traz ISPB.

Sobre crédito: "Pix no crédito" para conta própria é como se saca o limite do cartão. É risco de crédito (o cliente que usa todo o limite e some), não fraude de Pix, e o payload nem diz de onde vem o dinheiro. Fica fora do motor; vale uma frase se a banca perguntar.

### K. Concorrência e idempotência, do começo

**Idempotência** quer dizer que o mesmo pedido, repetido, produz efeito uma vez só.

Ela existe por causa de um cenário comum: o banco chama a API, a API grava, a rede cai antes de a resposta chegar e o banco reenvia. Sem idempotência:
- o mesmo Pix vira dois registros;
- o histórico conta em dobro;
- a segunda análise pode dar outro status, porque o histórico agora inclui a primeira.

O `id` do Pix é a chave de idempotência; não precisa de header novo.

| Chega | Resposta | Por quê |
|---|---|---|
| `id` novo | analisa, grava e responde **201** | caminho normal |
| `id` já visto, mesmo conteúdo | devolve a análise gravada com **200**, sem recalcular | recalcular daria outro resultado, porque o próprio Pix já está no histórico |
| `id` já visto, conteúdo diferente | **409** (`QIT001002`), sem sobrescrever | é outro pedido usando um id ocupado |

"Mesmo conteúdo" quer dizer o mesmo Pix depois de normalizado: CPF com ou sem máscara, `Z` ou `-03:00`, chaves em outra ordem, `150000` ou `150000.0`. Comparar os bytes do corpo daria 409 para um retry legítimo. O que se compara é o hash do payload normalizado:

```python
def payload_hash(pix: dict) -> str:
    normalized = normalize(pix)  # documentos sem máscara, datas em UTC, amount int, geolocation null removido
    text = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()
```

Testado: o exemplo oficial e a versão com `150000.0`, data em `Z`, CPF sem máscara e chaves em outra ordem dão o mesmo hash; mudar um centavo muda o hash.

**Corrida 1: o mesmo `id` ao mesmo tempo.**

```text
r1:  procura o id → não existe ─────────── INSERT ─── COMMIT
r2:       procura o id → não existe ─────────── INSERT ─ espera r1 ─→ erro de chave única
```

Os dois passam na checagem porque ninguém gravou ainda. É a corrida clássica de "verifica, depois age". A `UNIQUE(external_id)` impede o segundo registro: o Postgres segura o INSERT do r2 até o r1 terminar e então acusa a duplicidade. No teste, o r2 ficou parado até o commit do r1 (pouco mais de 1 segundo) e voltou sem inserir.

Sem tratamento, o SQLAlchemy levanta `IntegrityError` e a base responde 500. O controller captura, faz rollback, relê pelo `id` e devolve replay ou 409.

**Corrida 2: o mesmo pagador, Pix diferentes, ao mesmo tempo.**

```text
r1 (R$ 900):  lê o acumulado da noite = 0 → aprova → grava
r2 (R$ 900):  lê o acumulado da noite = 0 → aprova → grava      total R$ 1.800, limite R$ 1.000
```

Nenhuma constraint pega isso: os ids são diferentes e os dois registros são válidos. É assim que se contorna um limite, disparando em paralelo.

A solução é serializar só o mesmo pagador. `pg_advisory_xact_lock` é um cadeado com nome, preso à transação e solto sozinho no commit ou no rollback. Pagadores diferentes seguem em paralelo; o mesmo pagador entra em fila.

```python
self.session.execute(
    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
    {"key": f"payer:{payer_document}"},
)
```

**Por que READ COMMITTED.** Nesse nível, cada comando enxerga o que já estava commitado quando ele começou. O r2 espera no cadeado; quando o r1 commita, a leitura seguinte do r2 já vê os R$ 900.

Em REPEATABLE READ, a "foto" do banco é tirada no primeiro comando da transação. Esse comando é o próprio pedido do cadeado, feito antes de o r1 commitar. O r2 ganha o cadeado e continua lendo a foto antiga.

Medido num Postgres 16, com dois Pix de R$ 900 disparados juntos:

| Modo | r1 | r2 |
|---|---|---|
| sem lock | viu R$ 0, aprova | viu R$ 0, aprova |
| lock + READ COMMITTED | viu R$ 0, aprova | viu R$ 900, 2FA |
| lock + REPEATABLE READ | viu R$ 0, aprova | viu R$ 0, aprova |

READ COMMITTED é o padrão do Postgres e do SQLAlchemy. A base não muda nada aqui; só não troquem.

**Por que não travar o destino também.** Um comerciante recebe milhares de Pix, e travar pelo destino serializaria todos eles (um ponto quente). As contagens do lado do destino, como `MULE_FAN_IN`, aceitam errar por um ou dois Pix simultâneos sem mudar a decisão de forma relevante.

**Cuidados:**
- busquem as evidências dos connectors **antes** de pegar o cadeado. Chamada externa com o cadeado preso para a fila daquele pagador;
- usem `SET LOCAL lock_timeout = '3s'` antes do cadeado, para não deixar requisição pendurada;
- a base cria o pool com `pool_size=5` (com o overflow padrão do SQLAlchemy, até 15 conexões), e cada requisição na fila segura uma delas.

## 3. Arquitetura no Base Service

### 3.1 O que a base já resolve

| Ponto levantado na v1 | Como a base resolve | O que fazer |
|---|---|---|
| Formato inválido vs. valor impossível | schema JSON antes da rota (400, `QIT000001`); regra de valor no controller (422) | DV, data inexistente e conta aberta depois da transação → 422 no controller |
| Campo desconhecido | `additionalProperties: false` | manter no schema do Pix |
| Formato e códigos de erro | `{title, description, translation, code}`; `error_verification` derruba a API se um código se repetir | erros novos em `custom_errors.py`, a partir de `QIT001001` |
| Log e rastreio | `x-request-id` em toda resposta; log de entrada e saída sem corpo | logar `key`, status, score e códigos, com documento mascarado |
| Gravação atômica | o controller dá `commit`; o middleware faz rollback e fecha a sessão | Pix e análise no mesmo commit |
| Resultado do 2FA | padrão `PUT /webhook/...` → 204 | `PUT /webhook/pix/transaction/{id}/two_factor` |
| Status | tabela de status por enumerador | `pix_analysis_status` |
| Serviços de fora | `connectors/` com timeout obrigatório e log de ida e volta | DICT e cadastro de dispositivos como connectors |
| Configuração | variável de ambiente, com padrão no `docker-compose.yml` | `HOME_ISPB`, `DICT_TIMEOUT_MS` e o modo do mock |
| Testes | por HTTP, sem importar `src/`, com dados aleatórios por teste | cenários de calibração como testes de integração |

### 3.2 O que precisa mudar ou entrar

1. **Uma pasta nova, `src/engine/`, para o motor:** contexto imutável, regras, política e decisão. Pode calcular a decisão a partir de um contexto pronto. Não pode falar com banco, chamar connector, saber de HTTP nem ler o relógio. Quem a chama é o controller. Acrescentem essa linha na tabela de `docs/como-o-projeto-e-organizado.md`.
2. **Instantes com fuso.** A base usa `TIMESTAMP` sem fuso e `datetime.now()`. Para `transaction_date` e `opening_date`, usem `TIMESTAMPTZ` no SQL e `DateTime(timezone=True)` no model. Sem isso, o `-03:00` se perde e a regra noturna erra.
3. **O schema JSON tem três armadilhas, todas testadas:**
   - `"type": "integer"` aceita `150000.0`, porque o padrão da biblioteca é o draft 2020-12. Convertam com `int()` no controller;
   - o resolver da base, `RefResolver(..., None)`, não resolve `$ref` interno (`#/definitions/...`): dá `Unresolvable JSON pointer`. Escrevam o schema sem `$ref`, como o anexo `post_pix_transaction.json`, já validado com esse resolver;
   - `format: date-time` não valida nada (a própria base avisa nos testes). Usem `pattern` e convertam a data no controller.
4. **`IntegrityError` no commit** vira 500 se ninguém tratar. O controller trata (item K).
5. **O cadeado por pagador** mora no repository (`lock_payer`) e é chamado pelo controller.
6. **`INTERNAL-TOKEN`.** A base recusa com 403 qualquer rota de negócio sem esse header, antes de chegar ao resource. Mantenham a regra, que é padrão da base, e ponham o header em todo `curl` do README. A base também desliga o Swagger, então o README é o único manual.
7. **Removam o `sample_entity`** (rotas, controller, repository, models, schemas e testes), mantendo o `health_check`.
8. **Geradores de teste:** `RandomGenerator.generate_cnpj()` só gera CNPJ numérico. Acrescentem um gerador alfanumérico.
9. **O `database.sql` roda uma vez só,** quando o banco nasce. Mudou o schema, rodem `docker compose down -v`. A banca sobe do zero, então para ela não há problema.

### 3.3 O caminho de um `POST /pix/transaction`

```text
requisição
  ↓ middlewares: request_id → log → INTERNAL-TOKEN (403) → sessão de banco
  ↓ resources/pix_transaction.py
  │     @SchemaHandler.validate("post_pix_transaction.json")                    → 400
  ↓ controllers/pix_transaction_controller.py
  │     normaliza e valida valores: DV, datas, amount                           → 422
  │     connectors/: DICT (pagador e quem recebe) e cadastro de aparelhos, com timeout
  │     repositories/: cadeado do pagador → o id já existe?                     → 200 ou 409
  │                    histórico de 90 dias do pagador + entrada do destino em 24h
  │     engine/: evaluate(contexto) → decisão                         (puro, sem banco)
  │     repositories/: grava Pix + análise → commit
  ↓ dtos/pix_analysis_dto.py: {key, status, metadata}
  ↓ resource: 201 (novo) ou 200 (replay)
```

### 3.4 Rotas e erros

| Rota | O que faz | Responde |
|---|---|---|
| `POST /pix/transaction` | analisa e grava | 201; 200 no replay; 400; 409; 422 |
| `GET /pix/transaction/{id}` | devolve a análise gravada | 200; 404 |
| `PUT /webhook/pix/transaction/{id}/two_factor` | registra o resultado do 2FA | 204; 404; 409; 422 |
| `GET /` e `GET /health_check` | vêm da base | sem token |

| Código | HTTP | Quando |
|---|---|---|
| `QIT001001` | 404 | Pix não encontrado |
| `QIT001002` | 409 | `id` já usado com outro conteúdo |
| `QIT001003` | 422 | CPF ou CNPJ com DV inválido |
| `QIT001004` | 422 | data que não existe no calendário |
| `QIT001005` | 422 | conta aberta depois da transação |
| `QIT001006` | 409 | resultado de 2FA para Pix que não pediu 2FA, ou já registrado com outro valor |
| `QIT001007` | 422 | 2FA confirmado antes do próprio Pix |

### 3.5 Modelo de dados (`database/database.sql`)

```sql
CREATE TABLE pix_analysis_status(
    id                          SERIAL PRIMARY KEY,
    enumerator                  VARCHAR(50) NOT NULL,
    created_at                  TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(enumerator)
);

INSERT INTO pix_analysis_status (enumerator) VALUES
('approved'),
('rejected'),
('two_factor_required');

CREATE TABLE pix_transaction(
    id                          SERIAL PRIMARY KEY,
    external_id                 VARCHAR(64) NOT NULL,
    payload_hash                CHAR(64) NOT NULL,
    amount                      BIGINT NOT NULL CHECK (amount > 0),
    transaction_at              TIMESTAMPTZ NOT NULL,
    payer_document              VARCHAR(14) NOT NULL,
    payer_branch                VARCHAR(4) NOT NULL,
    payer_account_number        VARCHAR(20) NOT NULL,
    payer_account_opened_at     TIMESTAMPTZ NOT NULL,
    payee_participant           CHAR(8) NOT NULL,
    payee_branch                VARCHAR(4) NOT NULL,
    payee_account_number        VARCHAR(20) NOT NULL,
    payee_account_opened_at     TIMESTAMPTZ NOT NULL,
    payee_document              VARCHAR(14) NOT NULL,
    device_id                   VARCHAR(128) NOT NULL,
    channel                     VARCHAR(32) NOT NULL,
    ip                          INET,
    latitude                    NUMERIC(8, 6),
    longitude                   NUMERIC(9, 6),
    raw_payload                 JSONB NOT NULL,
    received_at                 TIMESTAMPTZ NOT NULL DEFAULT(NOW()),
    UNIQUE(external_id)
);

CREATE INDEX pix_transaction_payer_idx ON pix_transaction (payer_document, transaction_at);
CREATE INDEX pix_transaction_payee_idx ON pix_transaction (payee_document, transaction_at);
CREATE INDEX pix_transaction_payee_account_idx
    ON pix_transaction (payee_participant, payee_branch, payee_account_number, transaction_at);
CREATE INDEX pix_transaction_device_idx ON pix_transaction (device_id, transaction_at);

CREATE TABLE pix_analysis(
    id                          SERIAL PRIMARY KEY,
    pix_analysis_key            CHAR(36) NOT NULL,
    pix_transaction_id          INTEGER NOT NULL REFERENCES pix_transaction(id),
    status_id                   INTEGER NOT NULL REFERENCES pix_analysis_status(id),
    score                       INTEGER NOT NULL,
    policy_version              VARCHAR(20) NOT NULL,
    metadata                    JSONB NOT NULL,
    context_snapshot            JSONB NOT NULL,
    created_at                  TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_analysis_key),
    UNIQUE(pix_transaction_id)
);

CREATE TABLE pix_two_factor_result(
    id                          SERIAL PRIMARY KEY,
    pix_transaction_id          INTEGER NOT NULL REFERENCES pix_transaction(id),
    passed                      BOOLEAN NOT NULL,
    confirmed_at                TIMESTAMPTZ NOT NULL,
    created_at                  TIMESTAMP NOT NULL DEFAULT(NOW()),
    UNIQUE(pix_transaction_id)
);
```

Decisões que valem uma linha no README:
- **Documentos normalizados.** Sem máscara e em maiúsculas: `VARCHAR(14)` serve para CPF e para CNPJ alfanumérico.
- **Duas tabelas, de propósito.** `pix_transaction` é o que chegou; `pix_analysis` é o que decidimos, em relação 1:1.
- **`metadata` é exatamente o que o GET devolve.** Por isso o GET nunca recalcula.
- **`context_snapshot` guarda os agregados e o estado das evidências usados na decisão.** Serve para auditoria e não sai na API.
- **`raw_payload` guarda o corpo original,** inclusive o fuso informado, que o `TIMESTAMPTZ` descarta.
- **As contas ficam como colunas do Pix.** O payload é uma fotografia, e cada decisão usa a fotografia do seu Pix (item I). A origem não tem coluna de ISPB porque é sempre a instituição (`HOME_ISPB`).
- **O resultado do 2FA é um fato à parte,** com data própria. A análise nunca muda.

### 3.6 O controller, passo a passo

```python
class PixTransactionController(BaseController):
    def analyze(self, payload: dict) -> tuple[dict, bool]:
        pix = self._normalize(payload)                     # DV, datas, amount int → 422
        evidence = self._collect_evidence(pix)             # connectors com timeout, sem cadeado

        self.pix_repository.lock_payer(pix.payer_document)
        existing = self.pix_repository.get_by_external_id(pix.external_id)
        if existing is not None:
            return self._replay_or_conflict(existing, pix.payload_hash), True

        history = self.pix_repository.payer_history(pix.payer_document, until=pix.transaction_at, days=90)
        inbound = self.pix_repository.payee_account_inbound(pix.payee_account, until=pix.transaction_at)
        decision = evaluate(build_context(pix, history, inbound, evidence), POLICY)

        transaction = self.pix_repository.create(pix)
        analysis = self.analysis_repository.create(transaction, decision)
        analysis_dto = PixAnalysisDTO.obj_to_dict(analysis)
        try:
            self.session.commit()
        except IntegrityError:                              # mesmo id, outro pagador, ao mesmo tempo
            self.session.rollback()
            existing = self.pix_repository.get_by_external_id(pix.external_id)
            return self._replay_or_conflict(existing, pix.payload_hash), True

        return analysis_dto, False
```

No resource, a única decisão é o status HTTP, como na base:

```python
@SchemaHandler.validate("post_pix_transaction.json")
def on_post(self, payload: dict) -> JSONResponse:
    controller = PixTransactionController()
    analysis, replayed = controller.analyze(payload)
    status_code = http_status.HTTP_200_OK if replayed else http_status.HTTP_201_CREATED
    return JSONResponse(content=jsonable_encoder(analysis), status_code=status_code)
```

O histórico vem como linhas: os Pix do pagador nos últimos 90 dias, com status e resultado do 2FA. As janelas são calculadas no motor, como na simulação anexa. Assim cada regra mora num lugar só e é testável sem banco. O volume é pequeno: um pagador tem dezenas ou centenas de Pix em 90 dias, lidos por índice. A entrada do destino em 24h (pagadores distintos e soma) é uma consulta à parte.

### 3.7 Testes

Testes de integração por HTTP, no estilo da base, em `tests/integration/pix_transaction/`:
- **`test_contract.py`:**
  - exemplo oficial → 201 com os três campos, e o GET igual;
  - 400 pelo schema, 422 por DV e por data;
  - 403 sem token e 404 para id desconhecido.
- **`test_idempotency.py`:** replay com 200 e o mesmo corpo; 409; payload equivalente (máscara, `Z`) tratado como replay.
- **`test_concurrency.py`:**
  - dois POSTs simultâneos do mesmo pagador → o segundo enxerga o primeiro;
  - o mesmo id em paralelo → um 201 e um 200, nunca 500.
- **`test_calibration.py`:** um teste por linha da tabela 4.3. Cada cenário usa CPFs aleatórios (`RandomGenerator.generate_cpf()`), então não precisa de `DbUtils.rollback()`.
- **`test_two_factor_webhook.py`:** 204; 409 para Pix aprovado; 422 para `confirmed_at` anterior ao Pix.

Os testes não importam `src/`, que é a regra da base. A simulação anexa é o oráculo dos status esperados.

Se quiserem testes de unidade do motor, ponham em `tests/unit/` e documentem como exceção. A própria base faz isso com o `db_utils.py`, que chama de "exceção honesta".

## 4. Política v2

### 4.1 Regras

Definições usadas na tabela:
- **Pagamento efetivado:** Pix aprovado, ou em 2FA com `passed` confirmado antes do Pix avaliado.
- **Exposição:** Pix aprovado, ou em 2FA ainda não recusado com `failed`.
- **Perfil:** ≥ 5 pagamentos efetivados em ≥ 3 dias distintos, todos com mais de 24h.
- **Favorecido estabelecido:** recebeu pagamento efetivado do cliente há 24h ou mais.
- **Noite:** das 20h às 6h, em America/Sao_Paulo. Depois da meia-noite, começa às 20h do dia anterior.
- **Janelas:** `(t − Δ, t]` pela `transaction_date`. Só entram Pix com data até `t`.

| Família | Regra · onda | Lógica e parâmetros | Por quê |
|---|---|---|---|
| Limite | `NIGHT_SOFT_LIMIT` · 1 | CPF→CPF de pessoas diferentes, à noite; valor ou acumulado da noite > R$ 1.000 → no mínimo 2FA | referência da Res. 142; reproduz o exemplo oficial; o acumulado impede fracionar |
| Limite | `NIGHT_HARD_LIMIT` · 1 | à noite, destino sem relação de 24h ou conta própria aberta há < 7 dias; acumulado da noite para esses destinos > R$ 3.000 → reprova | coação: o 2FA não protege quem é obrigado a autenticar (D, E) |
| Limite | `LOW_HISTORY_EXPOSURE` · 1 | pagador sem perfil: > R$ 1.000 → +15; > R$ 5.000 → +30 (CNPJ: ×5) | sem histórico, o valor é a única régua |
| Velocity | `VELOCITY_BURST` · 1 | só CPF; Pix do pagador em 10 min, com o atual e retries contados uma vez: ≥ 5 → +30; ≥ 10 → reprova | esvaziamento de conta ou robô (G) |
| Velocity | `NEW_PAYEE_FANOUT` · 2 | cliente com perfil; ≥ 3 favorecidos novos em 1h → +20 | quem toma a conta espalha o dinheiro |
| Montante | `DAILY_AMOUNT` · 1 | ≥ 2 Pix em 24h. Sem perfil: soma > R$ 5.000 → +20 (CNPJ: ×5). Com perfil: soma > 3× a maior soma diária efetivada → +20 | fracionamento que nenhum Pix isolado mostra |
| Montante | `STRUCTURING` · 2 | ≥ 3 Pix em 24h entre 85% e 100% de R$ 1.000, R$ 3.000 ou R$ 5.000 → +30 | valor logo abaixo de um corte é evasão |
| Montante | `PASS_THROUGH` · 3 | pagador recebeu de ≥ 5 pagadores distintos em 24h (≥ R$ 2.000) e está mandando ≥ 80% do que entrou → +30 | conta de passagem (C) |
| Perfil | `AMOUNT_ABOVE_PROFILE` · 1 | régua em três degraus (F); valor > 2× a régua → +15; > 5× → +30 | a régua por favorecido protege o aluguel |
| Perfil | `PAYER_FLAGGED` · 3 | mock do DICT sobre o pagador: mula/golpista em 90 dias com ≥ 2 notificadores → reprova | a conta que repassa dinheiro de vítima (C) |
| Contraparte | `RECENT_PAYEE_ACCOUNT` · 1 | conta de destino < 7 dias → +25; de 7 a 30 dias → +10 | mula usa conta nova; sozinha, não reprova |
| Contraparte | `NEW_PAYEE` · 1 | cliente com perfil e favorecido sem pagamento efetivado → +10 | sinal fraco, que só soma |
| Contraparte | `MULE_FAN_IN` · 1 | destino < 30 dias, ≥ 5 pagadores distintos em 24h e (valor ≥ R$ 500 ou entrada ≥ R$ 5.000) → +40 (destino CNPJ: +20) | concentração em conta nova; o filtro de valor protege o rateio |
| Contraparte | `RECURRING_PAYEE` · 1 | ≥ 3 pagamentos efetivados ao favorecido em dias distintos, o último há ≤ 60 dias → −20 (a família não fica negativa) | tira o aluguel da fricção sem desligar as outras famílias |
| Contraparte | `SELF_TRANSFER_NEW_ACCOUNT` · 2 | mesmo titular: não conta favorecido novo, e Perfil e Montante valem metade; destino < 7 dias → +15 e alerta | conta segura (J) |
| Contraparte | `COUNTERPARTY_RESTRICTED` · 2 | mock do DICT: restrição, ou mula/golpista em 90 dias com ≥ 2 notificadores → reprova | evidência independente e corroborada (B) |
| Contraparte | `COUNTERPARTY_REPORTED` · 2 | notificação aberta, marcação de um notificador ou marcação em 12 meses → +30 e alerta | suspeita ainda não corroborada |
| Contraparte | `COUNTERPARTY_CHECK_UNAVAILABLE` · 2 | consulta falhou ou passou do timeout → +10 | incerteza, sem presumir fraude (A) |
| Acesso | `DEVICE_CHANGE` · 2 | pagador com histórico em outro aparelho, este nunca visto e não autorizado no mock de cadastro → +20; piso de 2FA se valor > R$ 200 ou soma em aparelhos novos em 24h > R$ 1.000 | referências da IN 491 como política própria |
| Acesso | `RECENT_ACCOUNT_RECOVERY` · 3 | mock: recuperação de senha ou troca de telefone/e-mail nas últimas 24h → +25 | com aparelho ou favorecido novo, forma o padrão de tomada de conta |
| Acesso | `SHARED_DEVICE` · 3 | o mesmo `device_id` usado por ≥ 3 pagadores em 24h → +40 | fazenda de aparelhos ou conta operada por terceiros |

### 4.2 Como as regras viram decisão

1. **Bloqueios reprovam direto:** `NIGHT_HARD_LIMIT`, `VELOCITY_BURST` com 10 ou mais Pix, `COUNTERPARTY_RESTRICTED` e `PAYER_FLAGGED`.
2. **Pisos:** `NIGHT_SOFT_LIMIT` e `DEVICE_CHANGE` exigem no mínimo 2FA.
3. **O score soma os pontos por família, com teto:** Limite, Velocity, Montante e Perfil, 30 cada; Acesso, 40; Contraparte, 50.
4. **Faixas:** abaixo de 30 aprova; de 30 a 69 pede 2FA; a partir de 70 reprova.
5. **Vence o mais severo** entre faixa, piso e bloqueio. A metadata diz qual foi, em `decided_by`.

Como nenhum teto chega a 70, **nenhuma família reprova sozinha**: só combinação de famílias ou bloqueio explícito.

```python
def decide(results: list[RuleResult], policy: Policy) -> Decision:
    points_by_family = defaultdict(int)
    for result in results:
        points_by_family[result.family] += result.points
    score = sum(min(policy.caps[family], max(0, points)) for family, points in points_by_family.items())

    band = (Action.REJECTED if score >= policy.reject_at
            else Action.TWO_FACTOR_REQUIRED if score >= policy.two_factor_at
            else Action.APPROVED)
    floors = [(r.min_action, r.code) for r in results if r.min_action is not None]
    action, decided_by = max([(band, "SCORE")] + floors, key=lambda item: item[0])

    return Decision(action, score, dict(points_by_family), decided_by, results)
```

### 4.3 Cenários de calibração

Todas as linhas saem de `python3 politica_v2_simulacao.py`. Ao implementar, cada uma vira um teste de integração. "Perfil padrão" é um cliente com 6 Pix aprovados em dias distintos, o maior de R$ 400.

| Cenário | Resultado | Conta |
|---|---|---|
| Exemplo oficial (22h15, R$ 1.500, destino aberto há 7 dias, primeiro contato) | 2FA | piso noturno; 15 + 10 = 25 |
| O mesmo payload reenviado com ids novos | 2FA, 2FA, reprovado | 3º: R$ 4.500 na noite > R$ 3.000 |
| O mesmo payload às 14h | aprovado | 25 |
| CPF desconhecido, R$ 50 às 14h, conta antiga | aprovado | 0 |
| 1º aluguel de R$ 2.500, perfil padrão | 2FA | 30 (6,25×) + 10 = 40 |
| Aluguéis seguintes, com webhook `passed` | aprovados | 0; do 4º mês em diante, −20 de recorrência |
| Os mesmos aluguéis, sem webhook | 2FA todo mês | 40 |
| Aluguel às 22h, favorecido estabelecido | 2FA | piso noturno; o teto não se aplica |
| R$ 900 às 23h50 e R$ 900 às 0h10, perfil padrão | aprovado, depois 2FA | 25; depois 15 + 20 = 35, mais o piso |
| Aparelho novo + destino de 3 dias + 7× o maior Pix, às 14h | reprovado | 30 + 35 + 20 = 85 |
| O mesmo, com o aparelho autorizado no mock | 2FA | 65 |
| R$ 5.000 para conta própria aberta há 2 dias, às 14h | 2FA com alerta | 15 (metade de 30) + 40 = 55 |
| O mesmo, às 23h | reprovado | teto noturno |
| Coação: R$ 5.000 às 23h para favorecido novo com conta antiga | reprovado | teto noturno; sem ele, 40 → 2FA |
| Coação: 4 × R$ 1.000 às 23h para quatro contas | aprovado, 2FA, reprovado, reprovado | 25; 45; 75; 75 |
| 6º pagador em 24h para conta de 10 dias, R$ 800 | 2FA com alerta | 10 + 40 = 50 |
| O mesmo, com marcações de mula de 2 PSPs no mock | reprovado | bloqueio |
| 5 amigos × R$ 40 para conta antiga (rateio) | aprovado | `MULE_FAN_IN` exige conta com menos de 30 dias |
| 3 × R$ 990 no mesmo dia, pagador sem histórico | aprovado, aprovado, 2FA | `STRUCTURING` 30 |
| 10 Pix de R$ 50 em 9 minutos para pessoas diferentes | 5º em 2FA, 10º reprovado | 30; bloqueio |
| Folha PJ: 20 salários de R$ 2.500 em 10 minutos, dois meses | todos aprovados | rajada não vale para CNPJ |
| Mock do DICT fora do ar: R$ 50 / exemplo oficial às 14h | aprovado / 2FA | 10 / 35 |

### 4.4 Metadata

O exemplo oficial sai assim. Os valores estão em centavos, como o `amount`, e a metadata não traz documentos:

```json
{
  "key": "b3f1c2e0-7d4a-4c1e-9a52-1f0e6c9d2a77",
  "status": "two_factor_required",
  "metadata": {
    "policy_version": "2.0.0",
    "decided_by": "NIGHT_SOFT_LIMIT",
    "score": 25,
    "score_by_family": {"Limite": 15, "Contraparte": 10},
    "reasons": [
      {"code": "NIGHT_SOFT_LIMIT", "effect": "min_two_factor", "observed": {"night_total": 150000, "limit": 100000}},
      {"code": "LOW_HISTORY_EXPOSURE", "points": 15, "observed": {"amount": 150000, "threshold": 100000}},
      {"code": "RECENT_PAYEE_ACCOUNT", "points": 10, "observed": {"payee_account_age_days": 7}}
    ],
    "risk_type": "night_exposure",
    "customer_warning": "Pix noturno acima de R$ 1.000 para outra pessoa. A conta de destino foi aberta há 7 dias.",
    "evidence": {"dict_payee": "ok", "dict_payer": "ok", "device_registry": "ok"},
    "data_quality": []
  }
}
```

`risk_type` resume o motivo dominante: `night_exposure`, `coercion_exposure`, `account_takeover`, `counterparty`, `velocity`, `structuring` ou `unusual_amount`. O `customer_warning` é o texto que o banco pode mostrar no passo do 2FA.

## 5. Edge cases

Os que vêm da própria base (token, `integer` aceitando `150000.0`, `$ref` interno, `TIMESTAMPTZ`, `IntegrityError`) estão em 3.2.

**Contrato e validação**
- **CNPJ alfanumérico.** A emissão começou em 31/07/2026, e os dois formatos coexistem ([IRIB][cnpj-irib]; [Agência Gov][cnpj-gov]). Usem o código do item H e nunca convertam documento para número.
- **ISPB `00000000` é o Banco do Brasil.** Tratem como texto de 8 caracteres, nunca como `int`.
- **`amount`:** 0, negativo, string, `true` ou acima do teto → 400 pelo schema. `150000.0` passa no schema, e o controller converte.
- **Datas:**
  - sem fuso → 400, pelo `pattern`;
  - data que não existe (`2026-02-30T...`) → 422;
  - o mesmo instante em `Z` e em `-03:00` → mesmo resultado e mesmo hash;
  - conta aberta depois da transação → 422 (`QIT001005`). Na v1 era alerta; mudou para seguir a base, porque é um valor que não pode existir.
- **Campos de contexto inválidos**, como IP malformado ou geolocalização `(0,0)`, são tratados como ausentes: vão para `data_quality`, sem 4xx. É uma exceção de propósito à regra do 422, porque esses campos não definem identidade nem histórico.
- **IP:** IPv6 se compara por /64. CGNAT (`100.64.0.0/10`) e faixas privadas ficam fora de qualquer sinal de "IP compartilhado".
- **`id`:** o schema restringe a `[A-Za-z0-9_-]{1,64}`, o mesmo critério que a base usa no `x-request-id`. Aceitar no GET também a `key` da análise é barato e opcional.

**Tempo**
- **Fronteiras:** testem 19:59:59, 20:00:00, 05:59:59 e 06:00:00, e a janela de exatamente 10 minutos.
- **Pix fora de ordem:** só entram no histórico Pix com data até `t`. A simulação pegou isso: sem o filtro, o perfil de outubro contaminava um aluguel de maio.
- **Mesmo timestamp:** desempatem pela ordem de chegada.
- **A correção acumula estado.** O reenvio do exemplo oficial dá 2FA, 2FA e reprovado. A metadata mostra o acumulado, e o README avisa e explica como zerar (`docker compose down -v`).

**Antifraude**
- **Espiral de bloqueio** e **toque duplo:** retry conta uma vez na velocity (item G), e o toque duplo ganha `possible_duplicate` na metadata, sem pontos.
- **Micro-Pix para muitos favorecidos:** a velocity não ignora valores pequenos.
- **Banco que reenvia após o 2FA com id novo:** conta em dobro no acumulado. O README diz "não reenviar; reportar pelo webhook".
- **Falsos positivos clássicos** (aluguel, folha PJ e rateio) estão na tabela 4.3.

## 6. Plano até 12h

| Até | Marco |
|---|---|
| 23h30 | Decisões deste documento congeladas. Repositório criado a partir da base, `sample_entity` removido, `database.sql` e models novos |
| 1h | POST e GET ponta a ponta, com motor provisório; 400, 422, 409 e replay; testes de contrato verdes. Em paralelo, `engine/` com as regras da onda 1, conferidas contra a simulação |
| 3h | Cadeado e histórico ligados ao motor; cenários da onda 1 como testes de integração; teste de concorrência |
| 6h | Onda 2: connectors dos mocks com timeout, webhook do 2FA e metadata completa. Revezem o sono; quem apresenta dorme primeiro |
| 8h30 | README no modelo da banca, diagrama ER, script de demo e p95 |
| 10h | Onda 3 só se tudo acima estiver verde. Código congelado; clone limpo em outra máquina, seguindo só o README |
| 11h30 | Dois ensaios cronometrados, vídeo de backup da demo e link entregue no Slack |

## 7. README e apresentação

**README**
- Sigam o modelo da banca. A tabela de regras é a 4.1, com os mesmos códigos da metadata, e "Como as regras viram decisão" é a 4.2.
- "Como rodar e testar":
  - `docker compose up`;
  - os `curl` com o header `INTERNAL-TOKEN`;
  - `pytest`;
  - o script de demo.
- No fim, uma seção de premissas e limitações:
  - o mock imita a fonte real;
  - a noite é 20h–6h;
  - MEI fica de fora da regra noturna;
  - não há retenção;
  - o 2FA volta por webhook;
  - não há pré-checagem na digitação da chave.
- A análise atual vira `docs/pesquisa.md`.

**Apresentação (7 minutos)**
- **45 s: três riscos, três respostas.** Tomada de conta → 2FA; destino suspeito → evidência sobre quem recebe; coação → teto. A frase: "2FA prova identidade, não vontade".
- **1 min: arquitetura na base**, com o diagrama da 3.3.
- **3 min: demo.**
  - Aluguel: 2FA, webhook, aprovado.
  - Exemplo oficial em 2FA, e o 3º reenvio reprovado pelo teto.
  - Coação: R$ 5.000 às 23h, reprovado.
  - Mula: 2FA, e reprovado quando o mock tem marcação.
  - GET.
- **1 min: engenharia.** Idempotência, o cadeado com a tabela medida, snapshot e versão da política.
- **1 min: limitações e próximos passos.** Retenção de Pix, pré-checagem na digitação da chave, cache com evento para o pagador e a onda 3.

**Perguntas prováveis**
- Por que R$ 3.000?
- E se o DICT cair?
- Por que não travar também o destino?
- Por que regras e não machine learning?
- Como não travar aluguel, folha e rateio?
- O que acontece se o banco reenviar o Pix depois do 2FA?

## Fontes

Consultadas em 17/10/2026.

- [API do DICT 2.12.1 — Banco Central][dict]
- [BC aprimora o Pix — comunicado do Banco Central, 27/08/2021][bcb-2021]
- [BC limita opção de período noturno para transações via Pix — Jornal Contábil][jc-noturno]
- [Pix tem novas regras de segurança para prevenir fraudes — Tecnoblog][tecnoblog-2024]
- [Receita Federal começa a emissão de CNPJ alfanumérico — IRIB][cnpj-irib]
- [Implantação do CNPJ alfanumérico ocorrerá a partir de 31 de julho — Agência Gov][cnpj-gov]

O comportamento da base vem da leitura do `bootcamp-base-api` enviado (README, `docs/` e `src/`). Os testes de concorrência rodaram num PostgreSQL 16.15 local; os de schema, com `jsonschema` 4.26, que usa o mesmo draft 2020-12 da versão 4.17.3 da base.

[dict]: https://www.bcb.gov.br/content/estabilidadefinanceira/pix/API-DICT.html
[bcb-2021]: https://static.poder360.com.br/2021/08/BC-aprimora-pix-27ago2021.pdf
[jc-noturno]: https://jornalcontabil.com.br/noticia/bc-limita-opcao-de-periodo-noturno-para-transacoes-via-pix/
[tecnoblog-2024]: https://tecnoblog.net/noticias/pix-tem-novas-regras-de-seguranca-para-prevenir-fraudes/
[cnpj-irib]: https://www.irib.org.br/?p=162074
[cnpj-gov]: https://agenciagov.ebc.com.br/noticias/202607/implantacao-do-cnpj-alfanumerico-ocorrera-a-partir-de-31-de-julho
