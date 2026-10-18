# Revisão independente — etapa 5: API Pix

Revise a PR da branch `feat/pix-api` contra `origin/main` base
`3daa579ef040e49f3f242a1bf7b39d01f9aefabc`. O link e SHA exato submetido estão na
descrição da PR e no prompt entregue com ela; registre o HEAD efetivamente lido.

Correções e prompt de re-revisão: `docs/CORRECOES-REVISAO-PR5.md`.
Achados e correções da re-revisão: `docs/CORRECOES-REREVISAO-PR5.md`.

Objetivo: POST/GET oficiais com parser de bytes, tenant/ISPB pela credencial,
claims e inicial duráveis, providers fora de locks, revalidação até três ciclos,
commit atômico e replay imutável. SQL real e espera HTTP sujeitos ao mesmo orçamento
monotônico de 3 s. Não se entrega execução financeira, desafio 2FA, DICT remoto,
webhook público ou calibração da política.

Leia `docs/api-pix.md`, `docs/validacao-pr5.md`, `PLANO-IMPLEMENTACAO.md`,
`docs/storage-pix.md`, `docs/motor-pix.md`, `docs/providers-pix.md` e o guia V3 em
`docs/contexto/GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md` (§§3, 5, 7, 8 e 11.2).
O PDF oficial em docs/contexto define contrato/escopo; escolhas do guia e parâmetros
experimentais não são requisitos atribuídos ao PDF.

Critérios de aceitação:

- 201 só depois de commit; 200 replay/GET com DTO original, 409 tenant/ID/hash
  inclusive em voo; GET de reserva sem análise 404. Três status, dinheiro exato,
  body <=64 KiB e envelope de erros da Base API.
- Tenant pela credencial, sem aceitar instituição no corpo; cache/histórico/eventos
  isolados. ISPB de origem em configuração e auditoria, sem alterar o contrato.
- Reservar ID antes de providers, posse/fencing em ambos os claims e inicial que
  sobrevive à rejeição/falha. Ausência/falha/vencimento não significam limpeza.
- Locks numéricos de origem/destino; contexto reconstruído sob locks, `C_pre`
  recalculado, snapshots/versionamento e conhecimento respeitados. Soltar locks
  para novas consultas; máximo três ciclos e prazo único.
- Executado/2FA/decisão separados. Identidade bancária explicitamente vinculada;
  revalidar vínculo que chega durante enriquecimento, sem duplicação nem antecipar
  conhecimento. Pagamentos importados não viram análises fictícias.
- Rollback de SQL/flush/commit, classificação exata de unique, 503 de resultado
  desconhecido resolvido por retry do mesmo ID/corpo. Cancelar espera não autoriza
  worker a confirmar trabalho novo após deadline.

Arquivos principais: `src/resources/pix.py`, `src/controllers/pix_api.py`,
`src/controllers/pix_storage.py`, `src/utils/pix_deadline.py`,
`src/middlewares/internal_token.py`, `src/providers/enrichment.py`,
`src/providers/configuration.py`, `src/antifraud/dto.py`, `src/errors/pix.py`,
`tests/integration/test_pix_api.py`, `tests/unit/test_pix_http_configuration.py`.
Não há migration nova; migrations 0001/0002 e fontes devem permanecer intactas.

Reproduza os comandos completos de `docs/validacao-pr5.md` em stack descartável
`pix-api-review`. Não use ambiente de aplicação. Procure bugs, regressões, falhas
de segurança, inconsistências e testes ausentes. Para cada achado, informe
prioridade, arquivo/linha, requisito, gatilho e evidência reproduzível. Testes
verdes não provam ausência de bugs. Diferencie bug obrigatório desta etapa de
extensão fora do escopo. Não altere código, resolva threads, faça merge ou avance.
