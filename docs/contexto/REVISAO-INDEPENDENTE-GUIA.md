# Revisão independente do guia e da referência V3

Concluída em 17/10/2026. **Sem achado P1/P2 pendente para entregar o guia e a referência executável.** Esta conclusão não certifica uma API implementada, integração real, eficácia estatística ou conformidade regulatória integral.

## Independência e escopo

Foi usado um agente separado, iniciado **sem o histórico da conversa**. Recebeu somente o enunciado, a Base API, as restrições essenciais confirmadas do projeto e os arquivos finais para revisão. Foi orientado a não ler avaliações, respostas e documentos históricos anteriores. Não participou da implementação das correções; reportou achados e depois reexecutou as reproduções.

Artefatos revisados: [guia completo](GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md), [contrato](contrato_pix.py), [schema](post_pix_transaction.json), [motor/simulador](politica_v2_simulacao.py), [testes de contrato](test_contrato_pix.py) e [testes do motor](test_politica_pix.py). O ZIP foi consultado como referência e permaneceu intacto.

## Achados e resolução

| Prioridade | Achado reproduzido | Correção e rechecagem |
|---|---|---|
| P1 | Snapshot antigo `registered`, recebido depois de uma revogação, substituía o estado mais recente | Ordenação por `observed_at` e `source_version`; disponibilidade apenas filtra conhecimento. Repetida reprodução: revogação continua rejeitando. A mesma correção cobre reputação e regressão de cache |
| P2 | Variação de caixa em `participant` alterava hash e fragmentava a conta de destino | Maiúsculas na normalização; variações equivalentes produzem replay. Teste de hash e reprodução independente passaram |
| P2 | Tentativas de outros tenants entravam no fan-in do cliente atual | Histórico de comportamento e fan-in filtrados por tenant; cache também segregado. Reprodução anterior deixou de elevar o risco |
| P2 | NUL e surrogate Unicode isolado passavam na entrada, mas não poderiam ser persistidos em JSONB | Recusa com 400 em strings/chaves, inclusive escapadas; Unicode válido e emoji permanecem aceitos |
| P2 | Data do ano 0001 passava na validação e causava overflow no motor | Faixa técnica UTC de transação/relógio de referência entre anos 0002 e 9998; entrada fora da faixa retorna 422, sem depender da data atual |
| P2 | Duas chamadas do mesmo ID ainda em voo podiam consultar providers antes de o banco resolver a duplicidade | Guia passou a exigir reserva `analysis_claim` por tenant/ID/hash antes de providers, com lease, fencing, espera/503 e conclusão na transação final. Revisado como desenho; ainda requer teste PostgreSQL na implementação |

Também foi esclarecido que a rota deve ler o corpo bruto antes da conversão automática do FastAPI, para não perder frações monetárias antes de chamar o parser estrito.

## Evidências executadas

Comando executado pelo agente principal e pelo revisor:

```bash
tmp/revisao-v2/.venv/bin/python -m unittest -q test_contrato_pix.py test_politica_pix.py
```

Resultado: **92 testes passaram — 36 de contrato e 56 do motor**, usando `jsonschema==4.17.3`, versão da base fornecida. Além da suíte, o revisor repetiu as cinco reproduções de código listadas acima; o sexto achado foi rechecado no algoritmo transacional do guia.

O agente principal também conferiu:

* [JSON da demonstração](politica_simulacao_resultados.json): 59 observações determinísticas, iguais ao resultado recalculado dos cenários. Não são 59 exemplos independentes de fraude real nem medida de precisão.
* Links locais do guia e hashes das cópias originais preservadas.
* ZIP original intacto, SHA-256 `764a8bd991df9bd1a1e879733a17af886682abbb0d9ab97384de8b08adfc7216`.
* As 16 observações históricas continuam reproduzíveis sobre os **originais** em `revisao-v2/originais`. Esse diagnóstico antigo não é uma suíte de aceitação da V3.

## Limites da conclusão

Ainda não foram executados testes HTTP ou PostgreSQL da nova API, porque ela não foi implementada nesta entrega. Locks, leases, fencing, recuperação de commit, migrations e conectores reais continuam dependentes da implementação e dos testes de integração especificados no guia.

Os números de score e os cortes monetários são parâmetros experimentais para o hackathon. O revisor confirmou a distinção entre política própria, limite operacional bancário, mock e dado real; não afirmou calibração estatística nem homologação pelo Banco Central.
