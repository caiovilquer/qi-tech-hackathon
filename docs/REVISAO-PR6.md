# Prompt de revisão independente — etapa 6

Revise a última etapa do projeto Antifraude Pix, branch `test/pix-delivery`,
base `21a3938defe320ebd7f0f30a51911ca0b1798371` de `origin/main`.
Confirme a identidade publicada antes de revisar:

```bash
gh pr view test/pix-delivery --json url,headRefOid,baseRefName,headRefName
git fetch origin --prune
git rev-parse origin/test/pix-delivery
```

Use o `headRefOid` completo como SHA da submissão; não confunda o commit de
implementação validado em cópia limpa com commits posteriores só de evidências.
O link e o SHA final também constam na descrição da PR e na mensagem de entrega.

Objetivo: completar aceitação/demonstração do guia V3 §§11.2/11.3 e documentação
oficial, sobre a API já integrada da etapa 5. Não muda regras, contrato público,
migrations, dependências nem promete integração bancária real. Não avance outra
etapa nem faça merge. Procure bugs e lacunas mesmo com os testes passando.

Leia README, PLANO-IMPLEMENTACAO.md, docs/aceitacao-pix.md,
docs/validacao-pr6.md, docs/api-pix.md e as fontes vigentes em docs/contexto
(guia V3, PDF oficial páginas 19–25 e referência Python V3). Documentos antigos
são histórico. Diferencie exigências oficiais, decisões do guia e experimentos.

Arquivos principais do diff: scripts/demo_pix.py,
tests/integration/test_pix_delivery.py, compose.test.yml,
.github/workflows/tests.yml, README.md e documentação final.

Critérios:

- Clone limpo reproduz build, migrations, suíte e demo sem arquivos locais.
- Demo sobe aplicação real em intérprete separado, com fixtures explícitas
  carregadas pela configuração existente e sem substituir controller/motor.
- Exercita primeiro Pix, troca legítima de aparelho com controle desconhecido,
  rajada de IDs distintos, noite acumulada cruzando meia-noite, destinatário
  adverso/restrito, replay/reinício e 2FA seguido de execução.
- Decisão original imutável; autenticação não cria execução nem cadastro de
  dispositivo; execução recém-conhecida não amadurece retroativamente.
- Runner só migra/remove o banco aleatório criado por ele; recusa ambiente
  inadequado antes de conectar, encerra seus processos e limpa falhas normais.
- 21 análises/Pix e 6 checagens iniciais, sem duplicação nos replays; status e
  razões observados refletem V3. Não usar JSON fixo como substituto da API.
- README no modelo oficial: integrantes, motor, 25 regras com parâmetros e
  racional, composição das decisões e comandos. Sem alegar eficácia,
  conformidade, DICT real ou garantia que os testes não demonstram.
- Matriz do §11.2 aponta evidência verificável na camada correta e admite limites.

Reproduza com Docker ativo, a partir da raiz deste Git, sem banco pessoal:

```bash
docker compose -p pix-review -f compose.test.yml up --build --abort-on-container-exit --exit-code-from tests tests
docker compose -p pix-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python tests/reference/verify_pix_reference.py --reference-dir /reference
docker compose -p pix-review -f compose.test.yml run --rm --no-deps -v "$PWD/docs/contexto:/reference:ro" tests python -m unittest discover -s /reference -p 'test_*.py' -q
docker compose -p pix-review -f compose.test.yml run --rm demo
docker compose -p pix-review -f compose.test.yml down -v --remove-orphans
```

O serviço demo também deve funcionar sozinho em um projeto Compose novo, sem
ter rodado testes/API previamente (`run --build --rm demo`). Não executar duas
suítes mutáveis simultaneamente no mesmo projeto. O usuário do servidor de teste
precisa de CREATE DATABASE; o Compose oferece isso apenas para dados sintéticos.

Procure bugs, regressões, falhas de segurança, inconsistências documentais,
falsas evidências e testes ausentes. Verifique cleanup, ambiente herdado,
deadlines, falhas de startup, temporalidade, retries/replays e separação entre
API real e serviço bancário simulado. Para cada achado, dê severidade, arquivo e
linha, evidência/reprodução, requisito afetado e correção mínima sugerida.
Não conclua ausência de bugs por concordar com a proposta ou pela suíte verde.
Registre no GitHub a revisão sobre o SHA exato e comente os achados individualmente.
