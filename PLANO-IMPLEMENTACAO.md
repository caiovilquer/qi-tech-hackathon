# Plano de implementação: Antifraude Pix

Referência vigente: guia V3 de 17/10/2026, política `pix-experimental-v3`.
Estado: PR 1 aprovada e mergeada em `c35005af40590fe9b60023ab29ac62d85bb9ff0f`.
PR 2 aprovada e mergeada em `1971e51fad28c38ad7e198db78f9ab73a1837b14`.
PR 3 aprovada e mergeada em `1e233ee4e5a02b3d7e7818eff1b1a27089690978`.
Etapa 4 aprovada e mergeada pela PR #5 em
`3daa579ef040e49f3f242a1bf7b39d01f9aefabc`.
Etapa 5 aprovada e mergeada pela PR #6 em
`21a3938defe320ebd7f0f30a51911ca0b1798371`.
Etapa 6 em `test/pix-delivery`, criada após pull de `origin/main` nesse SHA;
aceitação integrada, demonstração reproduzível e documentação final para revisão.

## Fontes e precedência

As fontes foram movidas da pasta pai para [docs/contexto](docs/contexto),
dentro deste Git, para permitir revisão em um clone independente. Ordem de
leitura e distinção entre vigente/histórico em [docs/README.md](docs/README.md);
inventário e SHA-256 em [docs/fontes.md](docs/fontes.md). Ler as fontes
vigentes antes de cada etapa. Os arquivos de referência não são implementação
de etapas futuras nem dependências de runtime.

1. **Oficial:** `Hackathon Antifraude Pix.pdf`, páginas 19–25: POST
   `/pix/transaction`, GET `/pix/transaction/{id}`, entrada oficial, topo
   `key/status/metadata`, três status, histórico próprio, Base Service,
   testes e README. Execução de Pix, frontend e implementação de 2FA não entram.
2. **Decisões do guia:** normalização, erros/HTTP, tenants pela credencial,
   análise imutável, DICT condicional, eventos, temporalidade, claims,
   locks, fencing e persistência atômica. Não são requisitos atribuídos ao PDF.
3. **Experimento:** pesos, cortes, janelas, TTL e orçamento são escolhas
   reproduzíveis do grupo. Score não é probabilidade nem calibração de fraude;
   fixtures não comprovam conformidade ou integração com DICT real.
4. **Referência executável:** `contrato_pix.py`, schema, motor de nome legado
   `politica_v2_simulacao.py` (conteúdo V3), duas suítes e resultados da simulação.
   Não substituir o banco pelo Simulator. A revisão independente abrange essa
   referência, não certifica a futura API. Documentos anteriores são histórico.

Não foi identificada contradição material entre contrato oficial e guia.
A regra de DV aceita os CPFs ilustrativos oficiais. Restrições adicionais
locais de parsing e valores estão identificadas no guia, seção 3.2.

## Inspeção e situação inicial

- Git já existente somente nesta subpasta, `main` limpa, commit inicial
  `baffeeead1f541d8d91ed3762a6a8c595d7f5bd9`.
- `origin`: `git@github.com:caiovilquer/qi-tech-hackathon.git`, repositório
  já **público**, branch padrão `main`. Não criar outro repositório, fazer deploy
  ou merge. Conferir novamente o destino antes de publicar.
- ZIP obrigatório: 85 entradas, 67 arquivos, sem `.git`; importação com remoção
  de exatamente um prefixo `bootcamp-base-api/`, conferida byte a byte.
  Commit dedicado `602d71c` preserva a base integral, inclusive documentação.
- Base real: FastAPI, JSON Schema 4.17.3, SQLAlchemy síncrono, psycopg2,
  PostgreSQL 16, Resource → Controller → Repository/Model → DTO, contexto,
  token, logger e session manager. `autoflush=False` exige flush explícito.
- Referência: **92 testes passaram**, sem skips, no venv existente da pasta
  pai; **59 observações** da simulação reproduzidas, sem alterar o JSON original.
  Python global não tinha jsonschema; isso era ambiente, não falha da referência.
- Docker estava desligado; iniciado para validação local. Bancos de teste
  dedicados, sem dados pessoais. Evidências da base e etapa em
  [docs/validacao-pr1.md](docs/validacao-pr1.md).

## Divisão e dependências

A ordem da seção 11.3 do guia é ajustada: regras e providers vêm antes da
exposição HTTP Pix. A publicação dos endpoints depende do protocolo completo
para evitar uma rota aparentemente pronta sem garantias de concorrência.
Cada etapa deve entregar e testar uma unidade utilizável; nenhuma exige
aprovar bugs obrigatórios para transferi-los à seguinte.

### PR 1 — `feat/pix-foundation`: base reproduzível e migrations

**Dependência:** nenhuma. **Escopo:** base original em commit separado;
migration inicial das três tabelas e quatro status já fornecidos; execução
versionada em bancos novos e adoção verificada em bancos da base existentes;
Compose com migration antes da API; volume persistente; ambiente de testes
isolado; CI e instruções. Manter camadas/middlewares e comportamento do exemplo.

**Aceitação:** health 204 e autenticação/erros preservados; criar/consultar
sample entity pelo HTTP e PostgreSQL real; dados sobrevivem ao reinício da API;
migration vazia, repetida e sobre base existente sem perda; DDL com falha faz
rollback; migrations concorrentes não duplicam execução; divergência em
migration aplicada é recusada. Testes destrutivos só em bancos descartáveis.

**Testes:** suíte original antes das mudanças; suíte original após mudanças;
testes PostgreSQL de migrations, adoção, integridade e rollback; smoke HTTP;
build e execução Compose; CI com PostgreSQL. **Riscos:** initializer do Docker
só roda em volume novo; adoção incorreta poderia mascarar schema incompatível;
testes da base derrubam `public`. Proteger execução de testes em ambiente separado.
**Limite:** sem tabelas de domínio, regra ou endpoint Pix; health é liveness.

### PR 2 — `feat/pix-engine`: contrato e motor puro versionado

**Dependência:** PR 1 aprovada. **Escopo:** portar parser exato/schema, tipos
imutáveis e regras em `src/antifraud`, registro de critérios e parâmetros
validados por tenant/versão. DTO público e snapshot auditável separados.
Sem importar Simulator como repository. Sem endpoint Pix ainda.

**Aceitação:** equivalência com referência nos cenários vigentes; CPF/CNPJ,
UTC, hash de todos os campos, centavos desde bytes, rejeição de duplicatas,
NaN, NUL/surrogates; famílias/tetos/pisos; `C_pre` usa mesmas regras locais;
configuração inválida falha no startup. Motor não acessa rede, banco ou relógio.

**Testes:** rastrear os 92 testes de referência: portar os 36 de contrato e
36 de política, mantendo os 20 de orquestração como referência das etapas futuras; testes puros de políticas/configuração, imutabilidade,
fronteiras e temporalidade. **Riscos:** copiar suposições do simulador;
perder precisão antes da validação; divergência entre score preliminar/final.

### PR 3 — `feat/pix-providers`: enriquecimento e contexto verificáveis

**Dependência:** PR 2 aprovada. **Escopo:** contratos BankContextProvider e
ReputationProvider, adapters determinísticos com fixtures explícitas e
validação; histórico importado com identidade bancária estável e separado de
análises/receipts da API; seleção
condicional DICT e ordenação de versões em funções testáveis. Sem rede real
ou serviços novos. Contratos assíncronos permitem cancelamento cooperativo;
I/O síncrono do banco continua destinado ao threadpool na futura integração.
`SourceCheck.OWNED/COMPLETED` é uma pré-condição do chamador, sem reserva em
memória fingindo durabilidade. Adapters reais devem usar os mesmos contratos quando houver
integração autorizada, sem fingir integração externa funcional.

**Aceitação:** ausência = not_covered; timeout = unavailable; expiração = stale;
escopo de tenant/conta/device/destino validado; observed_at/source_version
ordena, available_at filtra conhecimento; revogação não regride por atraso;
nenhum 2FA ou observação registra dispositivo. Contrato suporta deadline de
250 ms sem arredondar timeout para zero, orçamento total e no máximo um retry.

**Testes:** contratos compartilhados dos adapters; fixture ausente/expirada,
sujeito errado, versões conflitantes, cancelamento/revogação, evidência adversa
com C_pre alto; histórico futuro/tardio/maduro e reconciliação sem duplicação.
**Entrega e evidências:** [providers](docs/providers-pix.md),
[validação](docs/validacao-pr3.md), [prompt da PR 3](docs/REVISAO-PR3.md) e
[correções da revisão](docs/CORRECOES-REVISAO-PR3.md) e
[re-revisão](docs/CORRECOES-REREVISAO-PR3.md).
**Riscos:** indisponibilidade tratada como limpeza; confiança fora de escopo;
timeout de socket confundido com deadline total.

### PR 4 — `feat/pix-storage`: histórico, eventos e coordenação PostgreSQL

**Dependência:** PR 3 aprovada. **Escopo:** migrations incrementais e models/
repositories Pix/análise/receipt/cache/eventos; análise imutável; reservas
analysis_claim e source_initial_check com leases de 10 s e fencing distintos;
locks advisory ordenados numericamente; ingestão interna de eventos confiáveis.
Entregar APIs internas de persistência e coordenação testadas; não expor POST Pix.

**Aceitação:** constraints, índices e isolamento por tenant; posse única por
ID/hash antes de providers; takeover com token crescente; dono antigo não
conclui; consulta inicial por conta sobrevive a rejeição/indisponibilidade;
eventos idempotentes por tenant/provider/event_id, vínculo/fingerprint e
transições terminais; execução e 2FA separados; sem commits nos repositories
participantes do commit final, com exceção das transações curtas explícitas de
coordenação conduzidas pelo serviço. Análise/DTO atômicos e imutáveis.

**Testes:** PostgreSQL real: concorrência de claims e inicial; timeout/lease,
fencing; rollback entre inserts; eventos em ordem invertida/conflitantes;
migrations vazia/existente; índices/consultas de janela limitadas. **Riscos:**
ordem de locks, colisões, sessão após erro, adoção de eventuais schemas antigos.

**Entrega da etapa 4:** [storage e protocolo](docs/storage-pix.md),
[evidências](docs/validacao-pr4.md), [prompt independente](docs/REVISAO-PR4.md)
e [correções da revisão](docs/CORRECOES-REVISAO-PR4.md).

### PR 5 — `feat/pix-api`: fluxo HTTP completo e concorrente

**Dependência:** PR 4 aprovada. **Escopo:** resource fino recebe bytes com
limite de 64 KiB e envia I/O síncrono ao threadpool; controller integra contrato,
tenant por credencial e ISPB configurado, providers, regras e storage; POST e
GET oficiais, receipts mínimos, DTO congelado. Tratamento de flush/commit,
SQLSTATE e constraint específicos. Deadline total 3 s, lock_timeout 1 s,
até três ciclos de enriquecimento/revalidação. Sem webhook público extra.

**Aceitação:** nova análise 201 após commit, replay 200 sem providers,
conflito 409 inclusive em voo, GET por ID 200/404 no tenant; outros erros do
guia; nenhum quarto status. Reservar ID antes de providers, preservar hash
em falha técnica, revalidar sob locks de origem/destino e liberar locks para
novas consultas. Não retornar aprovação com contexto conhecido como vencido.

**Testes:** HTTP + PostgreSQL real para três decisões, todos os erros,
body limit e precisão; replay/tenant em processo reiniciado; IDs iguais e
hashes diferentes; mesma origem e múltiplas origens no limiar de fan-in;
evento concorrente que altera exposição; crash/takeover; flush/commit falho
e commit desconhecido resolvido pelo retry. **Riscos:** deadline real,
contenção e mudança externa após snapshot (limite explícito, sem promessa de
atomicidade com banco externo). Todos os cenários bloqueantes resolvidos nesta PR.

**Entrega da etapa 5:** [API e protocolo](docs/api-pix.md),
[evidências/TDD](docs/validacao-pr5.md), [prompt independente](docs/REVISAO-PR5.md),
[correções da revisão](docs/CORRECOES-REVISAO-PR5.md) e
[correções da re-revisão](docs/CORRECOES-REREVISAO-PR5.md).
Sem migration nova; 0001/0002 e fontes originais preservadas. A integração inclui
reconciliação de vínculos sob locks e snapshot completo de pagamentos importados;
é a concretização dos invariantes planejados, sem mudança de política experimental.

### PR 6 — `test/pix-delivery`: aceitação e documentação de entrega

**Dependência:** PR 5 aprovada. **Escopo:** ampliar validação integrada em
processos reais/reinício, casos da seção 11.2, demonstração determinística e
README final no modelo oficial, mapa de regras/razões e limites experimentais.
Esta PR complementa a evidência; não recebe bugs obrigatórios conhecidos de
etapas anteriores. Se houver achado, corrigir a etapa atual antes de aprová-la.

**Aceitação:** clone novo reproduz build/migrations/suíte/demo; cenários de
primeiro Pix, troca legítima de device, rajada, noite, adverso, replay e 2FA
seguido de execução; docs refletem os resultados sem prometer eficácia,
conformidade ou DICT real. **Testes:** matriz integral HTTP/PostgreSQL,
regressões, falhas de providers e recuperação; revisão de segurança/configuração.
**Riscos:** evidências de fixtures interpretadas como calibração; documentação
prometer funcionalidade que não foi exercitada.

**Entrega da etapa 6:** [matriz de aceitação](docs/aceitacao-pix.md),
[demonstração executável](scripts/demo_pix.py), [evidências/TDD](docs/validacao-pr6.md),
[prompt independente](docs/REVISAO-PR6.md) e
[correções da revisão](docs/CORRECOES-REVISAO-PR6.md). O README segue o modelo oficial,
com integrantes confirmados, 25 regras, parâmetros, racional e comandos.
Sem migration, alteração de contrato ou mudança da política nesta etapa.

**Revisão final de legibilidade e observabilidade:** logs estruturados por evento com
request id, catálogo de erros Pix com título/tradução por código, SQL concentrado no
`PixRepository`, tenant resolvido em `utils/pix_tenants.py`, nomes descritivos no motor e
no coordenador e remoção dos comentários didáticos da base. Sem mudança de contrato,
política, migrations ou dependências ([registro](docs/revisao-final-observabilidade.md)).

## Gate de cada etapa

Correções e comportamentos novos seguem TDD: teste reproduzindo a falha,
correção mínima e refatoração com suíte verde. Para comportamento já correto
sem cobertura, registrar que o teste começou verde, sem fabricar um bug.

Uma branch e PR por vez, commits Conventional Commits em inglês e sem
Co-Authored-By. Testar, revisar o próprio diff, atualizar README e registrar
comandos/resultados/limites antes de abrir PR no origin conferido. Publicar o
SHA exato em revisão e preparar prompt independente. **Parar até o usuário
trazer a revisão e confirmar aprovação.** Verificar cada achado contra o código,
registrar corrigido/improcedente com evidência/pendente, corrigir na mesma PR,
retestar e preparar re-revisão com novo SHA. Responder diretamente a cada
comentário/thread na PR com o que foi feito, localização, SHA e evidência; para
achados improcedentes, justificar com evidência. O relatório local não substitui
as respostas no GitHub. A aprovação de cada rodada deve ficar registrada na
própria PR como review aprovada sobre o SHA final; aprovação verbal ou fora do
GitHub não encerra o gate. Não resolver threads ou fazer merge automaticamente.
