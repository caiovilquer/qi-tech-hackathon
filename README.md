# Antifraude Pix

**Integrantes:** Caio Vilquer Carvalho; Caio Cidade Rodrigues; Matheus Victor Barbosa dos Santos.

API sobre a Base API do Bootcamp QI Tech, com FastAPI, PostgreSQL 16 e política
**experimental** `pix-experimental-v3`. Entrega `POST /pix/transaction` e
`GET /pix/transaction/{id}`, histórico próprio, análise imutável, idempotência
concorrente, providers simulados e eventos bancários internos. A etapa final está
em revisão; [plano e histórico das PRs](PLANO-IMPLEMENTACAO.md).

## Como o motor de decisão funciona

O resource recebe os bytes HTTP; o parser preserva dinheiro em **centavos inteiros**,
valida o contrato e calcula o hash de todos os campos normalizados. A credencial
`INTERNAL-TOKEN` determina o tenant no servidor. O controller reserva tenant/ID/hash
antes de consultar providers. Repetição igual devolve a análise original; outro
hash retorna conflito, inclusive durante uma chamada concorrente.

O enriquecimento reúne histórico próprio e pagamentos bancários confirmados,
segurança de conta/dispositivo e reputação. A primeira solicitação válida distinta
observada por conta exige checagem do remetente, com resultado durável. O destinatário
é consultado quando `C_pre < 80`; evidência adversa vigente já conhecida também é
considerada acima desse corte. Ausência, indisponibilidade e vencimento são
incerteza, nunca reputação limpa. Observação de aparelho não autoriza seu cadastro.

O motor puro recebe `Context` e `Policy` imutáveis, sem banco, rede ou relógio.
O coordenador usa horário do PostgreSQL, revalida o contexto sob locks ordenados de
origem/destino e grava Pix, análise, claim concluída e receipt em um commit.
O JSON público original e o snapshot privado com política/fingerprint, dados e
motivos são preservados. GET/replay não recalculam a decisão.

Mantivemos as camadas Resource → Controller → Repository/Model → DTO, contexto,
autenticação, logger e session manager da base. O `Simulator` Python fornecido é
referência de teste; a API não o utiliza como repository.

## Regras

Valores monetários abaixo estão em **centavos**. Pontos, cortes e janelas são
hipóteses reproduzíveis para a demonstração, sem calibração empírica. A escolha de
valores redondos facilita testar fronteiras; não representa limite regulatório.
O código ativo está em [rules.py](src/antifraud/rules.py),
[features.py](src/antifraud/features.py) e [policies.json](src/antifraud/policies.json).

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
| `PAYER_BANK_RESTRICTED` | Restrição explícita vigente de remetente/documento/conta | Rejeição | Preservar decisão institucional independente de pontuação |
| `PAYEE_BANK_RESTRICTED` | Restrição explícita vigente de destinatário/documento/conta | Rejeição | Não ignorar contraparte bloqueada por score preliminar alto |
| `BANK_EFFECTIVE_LIMIT` | Contexto bancário válido informa limite operacional excedido | Rejeição | Separar limite efetivo de score e resultado de 2FA |
| `PAYER_DICT_ADVERSE` | Evidência vigente tem ≥1 fraude ou relato aberto | +30 | Contagem adversa pesa, mas não equivale a restrição oficial |
| `PAYER_DICT_RESTRICTED` | Evidência contém restrição explícita vigente e aplicável | Rejeição | Restrição possui fonte, sujeito e validade próprios |
| `PAYEE_DICT_ADVERSE` | Mesmo sinal adverso para destinatário | +30; mesmo documento compartilha máximo entre papéis | Não contar a mesma evidência duas vezes; documentos distintos são independentes |
| `PAYEE_DICT_RESTRICTED` | Restrição explícita vigente do destinatário | Rejeição | Impedir que bônus de contexto ou tetos ocultem restrição |
| `DICT_CHECK_UNAVAILABLE` | Consulta necessária sem evidência vigente: ausente, indisponível ou vencida | +10 uma vez | Desconhecimento não é limpeza; ainda permite pequeno Pix com risco agregado baixo |

O perfil usa **90 dias** para limitar memória comportamental. Só execuções
confirmadas e conhecidas entram; intenção e execução precisam ter pelo menos
**24h** em relação à transação atual para maturidade. O perfil geral exige
**5 pagamentos em 3 dias distintos**; o específico exige **2 execuções maduras
na mesma conta destino**, independentemente do geral. São mínimos experimentais
para evitar referência apoiada em uma única operação. Um pagamento maduro já
estabelece o destinatário para o coeficiente preliminar, sem cadastrar beneficiário.

O cache DICT usa TTL de **300s**, comprometimento entre reutilização e atualização
na demonstração; também respeita validade do provider e watermark de versão.
Ocorrência, conhecimento, validade e revisão de origem são dimensões separadas.
Versão vencida mais nova não permite retornar a uma evidência antiga limpa.

## Como as regras viram decisão

1. Aplicar regras locais e tetos por família: **Limite 30, Velocity 30,
   Montante 30, Perfil 30, Acesso 40, Contraparte 50**. Esses tetos evitam somar
   irrestritamente sinais correlacionados; Acesso/Contraparte têm maior margem
   para combinar sinais distintos. Não limitam pisos de decisão.
2. Calcular `C_pre = max(0, 100 − min(100, score_local + incerteza))`.
   Incerteza soma **20** por ausência de perfil geral, aparelho desconhecido e
   destinatário não estabelecido, limitada a **40**. A hipótese penaliza falta
   de contexto sem tornar a primeira transferência automaticamente proibida.
   **80** é o corte experimental para consultar destinatário; `C_pre` é ordinal,
   não probabilidade de legitimidade.
3. Incorporar reputação e reaplicar os mesmos tetos. Score **≥70** rejeita;
   **≥30** exige 2FA; abaixo disso aprova. O intervalo intermediário reserva
   confirmação para risco moderado nesta política, sem validação de eficácia.
4. Aplicar o piso mais severo de todos os motivos: restrições, revogação,
   frequência extrema e limite bancário podem rejeitar mesmo com zero pontos.

Análise antifraude, resultado de 2FA e execução financeira são separados.
Sucesso de 2FA não registra aparelho, não prova pagamento e não muda `status`
original. Eventos confiáveis de execução alimentam perfil e exposição futura;
a ingestão é interna, com vínculo exato e idempotência, sem webhook público Pix.

## Como rodar e testar

Requer **Docker com Compose v2** e daemon ativo. Execute na raiz deste Git.
Não precisa instalar Python no host. Valores padrão são exclusivamente locais.

```bash
docker compose up -d --build --wait
curl -i http://127.0.0.1:3000/health_check
```

Health esperado: **204**, liveness. A API publica `127.0.0.1:3000`, PostgreSQL
`127.0.0.1:5432`. Se ocupadas, use
`API_PORT=3300 DB_PORT=55432 docker compose up -d --build --wait`.
`.env` é opcional; [.env.example](.env.example) documenta variáveis fictícias.
Não usar o token/senha padrão em ambiente real.

Exemplo oficial com identidades fictícias; `amount=5000` significa R$ 50,00:

```bash
curl -i http://127.0.0.1:3000/pix/transaction \
  -H 'INTERNAL-TOKEN: default_token' -H 'Content-Type: application/json' \
  --data-binary '{"id":"pix-demo-001","amount":5000,"transaction_date":"2026-10-17T12:00:00Z","source_account":{"branch":"0001","account_number":"88213","opening_date":"2025-03-02T13:00:00Z","owner":{"document_number":"12345678909"}},"destination_account":{"participant":"60701190","branch":"3675","account_number":"10442","opening_date":"2026-10-10T12:00:00Z","owner":{"document_number":"98765432100"}},"source":{"device_id":"dvc-demo","channel":"app","ip":"198.51.100.23"}}'
curl -i http://127.0.0.1:3000/pix/transaction/pix-demo-001 \
  -H 'INTERNAL-TOKEN: default_token'
```

Saída tem exatamente `key`, `status`, `metadata`. Só existem três status:
`approved`, `two_factor_required`, `rejected`. Metadata contém versão, motivos,
score, `confidence_pre`, `decision_at`, qualidade e estado/ação de enriquecimento.
Nova análise retorna **201 após commit**; replay **200** com o mesmo DTO, sem
providers; alteração com mesmo ID **409 QIT002002**. GET consulta ID externo no
tenant e retorna 404 se não concluído. Sem token válido: 403, sem consulta SQL.
Limite incremental de corpo: **64 KiB**. Erros seguem envelope da Base API.

Falha técnica: **503 QIT002008**, repetir o mesmo ID/corpo, inclusive se o commit
ficou sem confirmação. Conflito de titularidade ou vínculo/evento bancário:
**409 QIT002010**, requer reconciliação institucional; hash permanece reservado.
[Contrato, erros e configuração completa](docs/api-pix.md).

### Testes com PostgreSQL real

A suíte original derruba `public`; use exclusivamente a stack descartável abaixo,
sem portas publicadas. Nunca aponte testes para banco de uso.

```bash
docker compose -p pix-tests -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-tests -f compose.test.yml run --rm --no-deps \
  -v "$PWD/docs/contexto:/reference:ro" tests \
  python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-tests -f compose.test.yml run --rm --no-deps \
  -v "$PWD/docs/contexto:/reference:ro" tests \
  python -m unittest discover -s /reference -p 'test_*.py' -q
docker compose -p pix-tests -f compose.test.yml down -v --remove-orphans
```

Validação local em **18/10/2026**, após as correções da revisão independente:
**448 testes passaram em 86,82s** (Linux x86_64, Docker 29.8.2), PostgreSQL **16**
e Python **3.11** nas imagens, sem skips/xfails. São os 445 casos submetidos mais
3 da revisão. Antes das correções, o autor registrou **445 em 40,38s** (macOS) e
um clone limpo via origin com **445 em 40,19s**, demo isolada e exemplo HTTP do
README, inclusive persistência após restart. Referência separada: **92 testes**,
**59 observações** e **2.000 contextos** com Decision V3 completa equivalente.
Um warning de depreciação pertence ao `declarative_base` original.
Detalhes e reprodução em [validacao-pr6.md](docs/validacao-pr6.md) e
[CORRECOES-REVISAO-PR6.md](docs/CORRECOES-REVISAO-PR6.md).

O primeiro comando preserva o exit code do pytest. Não executar suítes simultâneas
sobre o mesmo projeto Compose. A guarda exige `APP_ENV=testing`, DB `pix_test_` e
confirma por sentinela que a API usa o mesmo banco antes de coletar os testes.
O usuário sintético de testes precisa poder criar/remover bancos isolados.

### Demonstração dos sete cenários

```bash
docker compose -p pix-demo -f compose.test.yml run --build --rm demo
docker compose -p pix-demo -f compose.test.yml down -v --remove-orphans
```

O comando inicia apenas o PostgreSQL de demonstração e um runner; o runner cria
um banco aleatório, aplica migrations, sobe Uvicorn em processo filho e chama
POST/GET reais. Ele gera fixtures normalizadas pelo contrato existente e usa os
adapters da aplicação, sem substituir controllers. Ao terminar, encerra seus
processos, remove somente o banco criado e os temporários, inclusive em falha
de cenário com a API viva e em SIGTERM (`docker stop`). Não modifica o banco
informado em `DATABASE_URL`. SIGKILL do container exige o `down` da stack
descartável. Se a API filha não subir, o erro traz a saída do Uvicorn.

A saída JSON valida: primeiro Pix; troca registrada de aparelho com controle
não registrado pelo banco; dez IDs em rajada; exposição noturna atravessando
meia-noite; destinatário adverso/restrito; replay após reinício; e 2FA seguido de
execução bancária. São **21 análises**, **6 checagens iniciais**, **1 evento de
autenticação e 1 de execução**. O evento é inserido pelo serviço interno confiável
do banco simulado, sem implementar autenticação 2FA ou movimentar dinheiro.
O Pix seguinte ao executado é datado **25h** depois: pela data da transação o
pagamento já caberia na janela de maturidade de 24h e só continua imaturo porque
a execução se tornou conhecida agora. Status/contagens são determinísticos;
UUIDs e horários vêm do processo/banco.

[Evidências, resultados e TDD](docs/validacao-pr6.md),
[matriz de aceitação do guia §11.2](docs/aceitacao-pix.md),
[prompt de revisão independente](docs/REVISAO-PR6.md) e
[correções da revisão](docs/CORRECOES-REVISAO-PR6.md).

### Ambiente e persistência

```bash
docker compose logs migrate api
docker compose restart api
docker compose down
```

O volume `postgres_data` sobrevive ao restart e `down`. **Não executar `down -v`
no ambiente de uso.** Depois de alterar código/migrations, reconstruir com
`up -d --build --wait`; restart não lê alterações do host. O Compose não monta
`src/` por bind mount. Migrations 0001/0002 são imutáveis, com checksum, transação
DDL e advisory lock; a adoção de base antiga valida schema antes de preservar
seus dados. [Procedimentos de migration e adoção](docs/migrations.md).

Para várias instituições, configurar `PIX_TENANTS` e políticas correspondentes no
servidor. Padrão: tenant sintético `demo`, ISPB `00000000` e `INTERNAL_TOKEN`.
Sem `PIX_FIXTURE_FILE`, os providers retornam `not_covered`. Arquivo de fixture
é configuração local do servidor; nunca é recebido pelo HTTP. Consultas operam
fora dos locks; leases de **10s** e fencing separados protegem ID e checagem
inicial. Budget de **3s**, tentativa de provider até **250ms**, um retry técnico,
lock até **1s** e até **3 ciclos** equilibram espera e revalidação na demonstração;
não representam SLA medido em produção. Pools Pix/Base são separados.

Desenvolvimento opcional exige Python **3.11**, como Docker/CI:

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt -c requirements.lock
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests/unit -p 'test_pix*.py' -q
```

O lock fixa dependências Python diretas/transitivas; imagens usam tags de série,
portanto o build não é hermético em binários. Não se adicionaram serviços, filas,
Redis ou dependências nesta etapa. O CI executa suíte PostgreSQL, comparação com
V3 e demo; seus resultados são evidência de execução, não ausência de bugs.

## Escopo e fontes

O PDF oficial exige contrato, três status, histórico, Base Service, testes e este
README. Normalização, DICT condicional, eventos, locks e protocolos são decisões
do [guia V3](docs/contexto/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md). Pesos, cortes,
janelas e TTL são **experimentais**, sem promessa de calibração, eficácia,
conformidade Pix, operação de DICT real ou atomicidade com banco externo.
Não há onboarding, execução financeira, frontend ou integração bancária real.
Rotas Sample Entity foram preservadas da base, fora do domínio Pix.

[Índice das fontes e revisões](docs/README.md), [hashes originais](docs/fontes.md),
[contrato do motor](docs/motor-pix.md), [providers](docs/providers-pix.md) e
[storage/eventos](docs/storage-pix.md). Documentos de cada PR registram a entrega
naquela data; o estado integrado atual é este README e a matriz final. O ZIP e os
24 documentos recebidos ficam em `docs/contexto`, sem dependência de runtime.
Git e arquivos temporários ficam separados; não inicializar Git na pasta pai.
