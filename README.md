# Antifraude Pix

**Integrantes:** Caio Vilquer Carvalho; Caio Cidade Rodrigues; Matheus Victor Barbosa dos Santos.

API construída sobre a Base API do Bootcamp QI Tech (FastAPI, PostgreSQL 16) que recebe
um Pix, aplica a política **experimental** `pix-experimental-v4` e devolve `approved`,
`two_factor_required` ou `rejected`. Entrega `POST /pix/transaction` e
`GET /pix/transaction/{id}`, histórico próprio em banco, análise imutável, idempotência
concorrente, providers simulados e eventos bancários internos.

## Como o motor de decisão funciona

1. **Borda HTTP.** O resource lê os bytes do corpo (até 64 KiB), o parser preserva o
   valor em **centavos inteiros**, valida o contrato oficial e calcula o hash SHA-256 de
   todos os campos normalizados. A credencial `INTERNAL-TOKEN` identifica a instituição
   (tenant) no servidor; nada de tenant no corpo.
2. **Reserva.** O controller reserva tenant + ID + hash antes de consultar qualquer
   provider. Repetição idêntica devolve a análise original (200); o mesmo ID com outro
   conteúdo recebe 409, inclusive durante uma chamada concorrente.
3. **Enriquecimento.** Fora de locks, reúne histórico próprio, pagamentos bancários
   confirmados, segurança de conta/aparelho e reputação. A primeira solicitação válida
   de cada conta exige a checagem do remetente, com resultado durável. O destinatário é
   consultado quando `C_pre < 80`; evidência adversa já conhecida vale mesmo acima do
   corte. Ausência, indisponibilidade e vencimento de dados são **incerteza**, nunca
   reputação limpa. Observar um aparelho não o cadastra.
4. **Decisão.** O motor puro recebe `Context` e `Policy` imutáveis, sem banco, rede ou
   relógio. O coordenador usa o horário do PostgreSQL, revalida o contexto sob locks
   ordenados de origem/destino e grava Pix, análise, reserva concluída e recibo em um
   único commit. O JSON público e o snapshot privado de auditoria (política, dados,
   motivos e features) são preservados; GET e replay nunca recalculam.

As camadas Resource → Controller → Repository/Model → DTO, o contexto por requisição,
a autenticação, o logger e o session manager da base foram mantidos. O Pix usa um pool
próprio e transações explícitas no `PixStorage`; todo SQL fica no `PixRepository`.
O `Simulator` Python fornecido é referência de teste, não repository da aplicação.

## Regras

Valores em **centavos**. Pontos, cortes e janelas são hipóteses reproduzíveis para a
demonstração, sem calibração empírica; valores redondos facilitam testar fronteiras.
Código em [rules.py](src/antifraud/rules.py), [features.py](src/antifraud/features.py)
e [policies.json](src/antifraud/policies.json).

| Regra / motivo | Lógica | Parâmetros iniciais | Por quê |
|---|---|---|---|
| `NIGHT_SOFT_LIMIT` | PF para outra PF, exposição na noite + atual acima do corte: piso 2FA | 100.000; 20h–6h, São Paulo | Pedir confirmação quando a exposição noturna cresce, inclusive atravessando meia-noite |
| `NIGHT_HARD_LIMIT` | Exposição qualificada na noite + atual acima do corte: rejeição | 300.000; PF para outra PF sem designação bancária de confiança, ou conta própria com menos de 7 dias | Conter exposição acumulada; autenticação não supera esse patamar próprio |
| `LOW_HISTORY_EXPOSURE` | Sem perfil geral nem específico, valor acima dos patamares | >100.000: +15; >500.000: +30; PJ ×5 nos valores | Pouca referência torna valores grandes mais incertos; escala PJ permite demonstrar fluxos maiores |
| `VELOCITY_BURST` | Quantidade de IDs distintos por horário da transação, incluindo atual | 10 min; PF ≥5: +30, ≥10: rejeição; PJ ≥25/100 | Detectar rajada sem confundir replay com tentativa nova; PJ tem maior frequência esperada na hipótese |
| `RECEIPT_VELOCITY` | Mesma contagem pelo horário de conhecimento da análise | Mesmos cortes; +30, compartilhando o máximo com a regra anterior | Detectar datas de transação espalhadas artificialmente sem duplicar o mesmo sinal |
| `DAILY_AMOUNT` | Soma de tentativas em 24h, inclusive rejeitadas; exige ≥2 tentativas | >500.000 sem perfil (PJ ×5), ou >3× mediana diária: +20 | Captar montante agregado; múltiplo relativo evita usar só um valor absoluto |
| `STRUCTURING` | ≥3 valores na faixa inclusiva de 85–100% de um corte em 24h | Cortes 100.000/300.000/500.000; +30 | Exercitar fracionamento perto dos patamares, incluindo fronteiras exatas |
| `AMOUNT_ABOVE_PROFILE` | Valor supera mediana madura específica, ou geral como fallback | >2×: +15; >5×: +30 | Mediana limita influência de um pagamento excepcional; dois níveis distinguem desvios |
| `RECENT_PAYEE_ACCOUNT` | Idade da conta destino no horário do Pix | <7 dias: +25; <30 dias: +10 | Distinguir conta muito nova de conta ainda recente, sem rejeitar só pela idade |
| `NEW_PAYEE` | Perfil geral existe, sem pagamento executado ao destino e sem mesma titularidade | +10 | Contraparte nova acrescenta incerteza moderada; recorrência não dá imunidade |
| `SELF_TRANSFER_NEW_ACCOUNT` | Mesmo titular para conta com menos de 7 dias | +15 | Mesmo documento não elimina risco de conta própria recém-aberta |
| `MULE_FAN_IN` | Conta destino <30 dias, ≥5 origens em 24h, valor atual ou soma elevados | Atual ≥50.000 ou soma ≥500.000; PF +40/PJ +20 | Combinar concentração e volume reduz sinal por simples compartilhamento de destino; pesos distintos são hipótese |
| `DEVICE_REVOKED` | Revogação bancária atual conhecida | Rejeição, sem pontos | Respeitar revogação explícita mesmo com score baixo |
| `UNREGISTERED_DEVICE` | Provider informa dispositivo não cadastrado | +20 | Cadastro é fato bancário, não inferência por uso anterior |
| `DEVICE_CHANGE` | Dispositivo desconhecido diferente de um observado no histórico da conta | +20 | Mudança sem confirmação bancária acrescenta risco; troca registrada é permitida |
| `DEVICE_POLICY_LIMIT` | Não cadastrado, ou troca desconhecida, excede patamar por operação; soma diária só para não cadastrado | >20.000 por operação ou >100.000 no dia local: piso 2FA | Pedir confirmação sem fingir implementar cadastro ou os limites oficiais de aparelho |
| `RECENT_ACCOUNT_RECOVERY` | Recuperação ocorrida nas últimas 24h e conhecida agora | +25 | Recuperação recente pode alterar confiança no acesso; a janela é hipótese operacional |
| `DEVICE_THEFT_REPORTED` | Contexto bancário válido informa roubo/perda comunicado para o aparelho, mesmo antes da revogação | Rejeição, sem pontos | Roubo não é inferido do comportamento; com o fato bancário, o app desbloqueado não movimenta a conta |
| `DURESS_REPORTED` | Contexto bancário válido traz sinal de coação da sessão (ex.: credencial de pânico) | Rejeição, sem pontos | 2FA pode ser concluído sob ameaça; só o banco pode observar a coação |
| `PAYER_BANK_RESTRICTED` | Restrição explícita vigente de remetente/documento/conta | Rejeição | Preservar decisão institucional independente de pontuação |
| `PAYEE_BANK_RESTRICTED` | Restrição explícita vigente de destinatário/documento/conta | Rejeição | Não ignorar contraparte bloqueada por score preliminar alto |
| `BANK_EFFECTIVE_LIMIT` | Contexto bancário válido informa limite operacional excedido | Rejeição | Separar limite efetivo de score e resultado de 2FA |
| `PAYER_DICT_ADVERSE` | Evidência vigente tem ≥1 fraude ou relato aberto | +30 | Contagem adversa pesa, mas não equivale a restrição oficial |
| `PAYER_DICT_RESTRICTED` | Evidência contém restrição explícita vigente e aplicável | Rejeição | Restrição possui fonte, sujeito e validade próprios |
| `PAYEE_DICT_ADVERSE` | Mesmo sinal adverso para destinatário | +30; mesmo documento compartilha máximo entre papéis | Não contar a mesma evidência duas vezes; documentos distintos são independentes |
| `PAYEE_DICT_RESTRICTED` | Restrição explícita vigente do destinatário | Rejeição | Impedir que bônus de contexto ou tetos ocultem restrição |
| `DICT_CHECK_UNAVAILABLE` | Consulta necessária sem evidência vigente: ausente, indisponível ou vencida | +10 uma vez | Desconhecimento não é limpeza; ainda permite pequeno Pix com risco agregado baixo |
| `BANK_AUTHORIZED_CONTEXT` | Teto: autorização bancária explícita para conta/aparelho/destino vigente (ex.: lote de folha cadastrado) | Rejeição só por score vira 2FA | Operação legítima atípica é confirmada, não bloqueada; não supera pisos nem reputação adversa |
| `SAME_HOLDER_TRANSFER` | Teto: mesmo CPF/CNPJ na origem e no destino | Rejeição só por score vira 2FA | O dinheiro fica com o titular; conta própria nova ainda pontua e o limite noturno rígido ainda rejeita |

O perfil usa **90 dias** de memória comportamental. Só execuções confirmadas e
conhecidas entram; intenção e execução precisam ter pelo menos **24h** em relação à
transação atual para contar como maduras. O perfil geral exige **5 pagamentos em
3 dias distintos**; o específico exige **2 execuções maduras na mesma conta destino**.
Um pagamento maduro já estabelece o destinatário para o coeficiente preliminar, sem
cadastrar beneficiário. O cache DICT usa TTL de **300s** e respeita validade e versão
do provider; ocorrência, conhecimento, validade e revisão da fonte são dimensões separadas.

## Como as regras viram decisão

1. **Regras locais e tetos por família:** Limite 30, Velocity 30, Montante 30,
   Perfil 30, Acesso 40, Contraparte 50. Os tetos evitam somar sinais correlacionados
   sem limite; não limitam pisos de decisão.
2. **Coeficiente preliminar:** `C_pre = max(0, 100 − min(100, score_local + incerteza))`.
   A incerteza soma **20** por ausência de perfil geral, aparelho desconhecido e
   destinatário não estabelecido, limitada a **40**. `C_pre < 80` dispara a consulta
   ao destinatário; `C_pre` é ordinal, não probabilidade.
3. **Reputação e score final:** reaplicam-se os tetos. Score **≥70** rejeita; **≥30**
   exige 2FA; abaixo disso aprova.
4. **Tetos:** se a rejeição vem só do score, sem piso de rejeição nem reputação adversa,
   uma autorização bancária explícita ou a mesma titularidade a transformam em 2FA. O
   motivo do teto só aparece quando mudou o resultado; o score continua o mesmo.
5. **Pisos:** o motivo mais severo prevalece. Restrições, revogação, roubo ou coação
   informados pelo banco, frequência extrema e limite bancário rejeitam mesmo com zero
   pontos; aparelho não cadastrado acima do corte exige 2FA mesmo com score baixo.

Análise antifraude, resultado de 2FA e execução financeira são fatos separados.
Sucesso de 2FA não registra aparelho, não prova pagamento e não muda o `status`
original. Eventos confiáveis de execução alimentam perfil e exposição futuras; a
ingestão é interna, com vínculo exato e idempotência, sem webhook público.

## Como rodar e testar

Requer **Docker com Compose v2** e daemon ativo. Execute na raiz deste Git; não é
preciso Python no host.

```bash
docker compose up -d --build --wait
curl -i http://127.0.0.1:3000/health_check          # 204
```

A API publica `127.0.0.1:3000` e o PostgreSQL `127.0.0.1:5432`
(`API_PORT=3300 DB_PORT=55432 docker compose up -d --build --wait` para trocar).
`.env` é opcional; [.env.example](.env.example) documenta variáveis fictícias.
Não use o token/senha padrão fora do ambiente local.

### Exemplo oficial

`amount=5000` significa R$ 50,00. Identidades fictícias.

```bash
curl -i http://127.0.0.1:3000/pix/transaction \
  -H 'INTERNAL-TOKEN: default_token' -H 'Content-Type: application/json' \
  --data-binary '{"id":"pix-demo-001","amount":5000,"transaction_date":"2026-10-17T12:00:00Z","source_account":{"branch":"0001","account_number":"88213","opening_date":"2025-03-02T13:00:00Z","owner":{"document_number":"12345678909"}},"destination_account":{"participant":"60701190","branch":"3675","account_number":"10442","opening_date":"2026-10-10T12:00:00Z","owner":{"document_number":"98765432100"}},"source":{"device_id":"dvc-demo","channel":"app","ip":"198.51.100.23"}}'
curl -i http://127.0.0.1:3000/pix/transaction/pix-demo-001 -H 'INTERNAL-TOKEN: default_token'
```

A resposta tem exatamente `key`, `status` e `metadata`. `metadata` traz versão da
política, `score`, `confidence_pre`, `reasons` (códigos da tabela acima), `decision_at`,
qualidade dos dados e o estado de cada consulta de enriquecimento. Toda resposta carrega
o cabeçalho `X-Request-ID`, que também aparece em cada linha de log daquela requisição.

| Situação | HTTP | Código |
|---|---|---|
| Nova análise (`approved`, `two_factor_required` ou `rejected`), após commit | 201 | — |
| Repetição do mesmo ID com o mesmo conteúdo (replay, sem providers) | 200 | — |
| Mesmo ID com conteúdo diferente, inclusive em voo | 409 | `QIT002002` |
| Titularidade ou vínculo bancário contraditório; reconciliar antes de repetir | 409 | `QIT002010` |
| JSON, estrutura, tipo ou formato inválidos (campo informado em `description`) | 400 | `QIT000001` |
| CPF/CNPJ com dígito inválido · data/fuso inválidos · conta aberta após a transação | 422 | `QIT002003` · `QIT002004` · `QIT002005` |
| Corpo acima de 64 KiB | 413 | `QIT002009` |
| GET de ID sem análise concluída no tenant | 404 | `QIT002001` |
| Sem `INTERNAL-TOKEN` válido (sem consulta ao banco) | 403 | `QIT000002` |
| Falha técnica ou dado que não estabiliza em 3s: repetir o mesmo ID e corpo | 503 | `QIT002008` |

Erros seguem o envelope da Base API (`title`, `description`, `translation`, `code`).
[Contrato completo, configuração e protocolo](docs/api-pix.md).

### Logs e observabilidade

Uma linha por evento, em inglês, no formato `evento chave=valor`, sempre com o
`X-Request-ID` entre colchetes. Texto vindo do cliente é escapado (sem injeção de linha).
Nunca se registra o corpo da requisição.

| Evento | Nível | O que informa |
|---|---|---|
| `request.started` / `request.finished` | INFO | método, caminho, status e `elapsed_ms` de cada requisição (health check fora) |
| `auth.denied` | WARNING | credencial ausente ou inválida |
| `pix.analysis.completed` | INFO | `pix_id`, `outcome` (analysis/replay), `status`, `key`, `score`, `confidence_pre`, `reasons`, ciclos de revalidação e `elapsed_ms` |
| `pix.analysis.error` | INFO | `http_status`, `code` e campo inválido de toda requisição Pix recusada |
| `pix.analysis.budget_exceeded`, `pix.analysis.reconciliation_required`, `provider.unavailable` | WARNING | motivo operacional por trás de um 503/409 e qual provider ficou indisponível |
| `pix.analysis.failed`, `pix.analysis.database_error`, `pix.receipt.*`, `error.unexpected` | ERROR | falhas técnicas, sempre com traceback |
| `pix.claim.busy`, `pix.enrichment.refresh`, `pix.context.refresh` | DEBUG | esperas por reserva e ciclos de revalidação |
| `error.response` | INFO | status e código de qualquer erro devolvido no envelope da base |

```bash
docker compose logs api | grep pix.analysis.completed
docker compose logs api | grep '<X-Request-ID da resposta>'
```

O acesso padrão do Uvicorn fica desligado para não duplicar as linhas de requisição.

### Testes com PostgreSQL real

A suíte usa uma stack descartável, sem portas publicadas. Nunca aponte testes para o
banco de uso.

```bash
docker compose -p pix-tests -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-tests -f compose.test.yml run --rm --no-deps \
  -v "$PWD/docs/referencia:/reference:ro" tests \
  python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-tests -f compose.test.yml down -v --remove-orphans
```

São **415 testes** (HTTP real, concorrência, processos, falhas SQL, tempo, providers,
migrations e aceitação) mais a comparação com a referência Python V3 (59 observações e
2.000 contextos com `Decision` equivalente). A guarda exige `APP_ENV=testing`, banco
`pix_test_` e confirma por sentinela que a API usa o mesmo banco antes de coletar: uma
análise mínima gravada direto no banco é lida por `GET /pix/transaction/{id}`. Registros
Pix são imutáveis, então a sentinela fica no banco descartável, identificada pelo prefixo
`sentinel-` e pela política `test-sentinel`.
A cobertura por grupo do guia e os limites da evidência estão em
[docs/aceitacao-pix.md](docs/aceitacao-pix.md).

### Demonstração dos sete cenários

```bash
docker compose -p pix-demo -f compose.test.yml run --build --rm demo
docker compose -p pix-demo -f compose.test.yml down -v --remove-orphans
```

O runner cria um banco aleatório, aplica as migrations, sobe o Uvicorn em processo filho
e chama POST/GET reais: primeiro Pix; troca registrada de aparelho com controle não
registrado; dez IDs em rajada; exposição noturna atravessando meia-noite; destinatário
adverso/restrito; replay após reinício do processo; e 2FA seguido de execução bancária
(o Pix seguinte, 25h depois, continua imaturo só porque a execução foi conhecida agora).
São **21 análises**, **6 checagens iniciais**, 1 evento de autenticação e 1 de execução.
Ao terminar, ou em falha e SIGTERM, encerra seus processos e remove só o banco criado.

### Ambiente e persistência

```bash
docker compose logs migrate api
docker compose restart api
docker compose down
```

O volume `postgres_data` sobrevive a restart e `down`; **não executar `down -v` no
ambiente de uso**. Após alterar código ou migrations, `up -d --build --wait`.
Migrations 0001/0002 são imutáveis, com checksum, transação DDL e advisory lock
([docs/migrations.md](docs/migrations.md)).

Para várias instituições, configurar `PIX_TENANTS` e políticas correspondentes.
Padrão: tenant `demo`, ISPB `00000000` e `INTERNAL_TOKEN`. Sem `PIX_FIXTURE_FILE`, os
providers retornam `not_covered`. Budget de **3s** por requisição, provider até **250ms**
com um retry técnico, leases de **10s**, lock até **1s** e até **3 ciclos** de revalidação;
não representam SLA medido.

Desenvolvimento opcional com Python **3.11**:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -c requirements.lock
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
```

## Escopo e fontes

O PDF oficial exige contrato, três status, histórico, Base Service, testes e este README.
Normalização, DICT condicional, eventos, locks e protocolos são decisões do
[guia V3](docs/referencia/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md). Pesos, cortes, janelas e
TTL são **experimentais**, sem promessa de calibração, eficácia, conformidade Pix ou
operação de DICT real. Não há onboarding, execução financeira, frontend ou integração
bancária real. As rotas de exemplo da base (Sample Entity e connector de boletos) foram
removidas; permanecem apenas health check e Pix.

[Índice da documentação](docs/README.md), [contrato do motor](docs/motor-pix.md),
[providers](docs/providers-pix.md) e [storage/eventos](docs/storage-pix.md). A referência
Python V3 e seus testes originais ficam em `docs/referencia/`.
