# Matriz de cenários Pix

Execução local em 18/10/2026 com o motor puro `antifraud.engine.evaluate`, política
`pix-experimental-v3` (fingerprint
`109c5a6a56398c05efa6b6c848f1d317175211c0e04321270a334d198067263f`).
O gerador compara os parâmetros da política embutida com
`src/antifraud/policies.json` antes da execução.

## Como reproduzir

```bash
PYTHONPATH=src:. python3 -m unittest tests.unit.test_pix_permutation_matrix -v
PYTHONPATH=src:. python3 -m tests.support.pix_permutation_matrix --output-dir reports
```

O primeiro comando executou **7 testes, todos passaram**. O segundo produziu
[`pix_permutation_matrix.csv`](pix_permutation_matrix.csv), com uma linha por
combinação, e [`pix_permutation_summary.json`](pix_permutation_summary.json).
O CSV é ignorado pelo Git conforme `.gitignore`, mas permanece no workspace.

O [gabarito proposto e a comparação](GABARITO.md) classificam todas as
combinações por expectativa de cenário. A comparação detalhada está em
[`pix_permutation_gabarito.csv`](pix_permutation_gabarito.csv) e os totais em
[`pix_permutation_comparison.json`](pix_permutation_comparison.json).

## Definições sintéticas

São `2 × 2 × 2 × 3 × 2 × 2 × 2 × 2 × 3 = 1.152` combinações por caso.
As 11 situações geram 12.672 linhas. As combinações semanticamente impossíveis
ou incompatíveis com a descrição do caso recebem `inaplicavel` e um motivo.

| Fator | Representação no teste |
|---|---|
| Horário | 14h ou 22h em `America/Sao_Paulo`. |
| Valor | Baixo R$ 50, médio R$ 150, alto R$ 1.200. O perfil histórico, quando existe, tem mediana R$ 150; alto equivale a 8 vezes essa mediana. |
| Origem recente/velha | Recente sem perfil geral; velha com seis pagamentos confirmados em 30 dias, suficientes para o perfil geral. Uma origem recente pode ter dois pagamentos anteriores ao mesmo destinatário quando `recorrente`. A data de abertura da origem não é um campo do `Pix` usado pelo motor. |
| Destino recente/velho | Aberto há 2 ou 180 dias. O destino velho também tem seis recebimentos confirmados de outras contas, entre 35 e 60 dias antes; eles ficam fora do fan-in de 24 horas. |
| Tentativas | Única sem outras tentativas na janela de 10 minutos; múltiplas com quatro tentativas anteriores para PF ou 24 para PJ, atingindo o limiar brando de velocidade da política. |
| Recorrência | Primeira sem pagamento anterior àquele destino; recorrente com dois pagamentos executados e maduros ao mesmo destino. |
| Localização | Ausente, coordenada habitual sintética em São Paulo ou coordenada distante sintética em Manaus. A localização é registrada no CSV, mas o motor não recebe geolocalização nem referência de local habitual. |
| Reputação | Evidências vigentes e explícitas em cache para origem e destino. Nos casos 6 e 7, o respectivo documento possui um relato aberto. Isto testa a decisão quando a evidência está disponível; não testa se a API consultaria o DICT nem representa os providers vazios padrão. |
| Dispositivo | Estado bancário atual `registered`. No caso 8 há análise, 2FA concluído e execução no telefone novo, seguidos de tentativa pelo antigo; o sucesso do 2FA não altera o registro do dispositivo antigo. |

Os pagamentos de perfil e recorrência incluem evento bancário `executed`. As
tentativas em rajada são análises anteriores; nos casos 3, 9 e 11 também incluem
execução confirmada. O caso 4 acrescenta quatro remetentes distintos com
pagamentos confirmados ao destino atual; a operação corrente é a quinta origem.

## Resultado

| Caso | Avaliadas | Inaplicáveis | `approved` | `two_factor_required` | `rejected` |
|---|---:|---:|---:|---:|---:|
| 1. Aparelho roubado, app desbloqueado | 1.152 | 0 | 369 | 555 | 228 |
| 2. Coação | 1.152 | 0 | 369 | 555 | 228 |
| 3. Envio legítimo a contas diferentes | 1.152 | 0 | 369 | 555 | 228 |
| 4. Recebimento legítimo de contas diferentes | 1.152 | 0 | 357 | 483 | 312 |
| 5. Pix genérico | 1.152 | 0 | 369 | 555 | 228 |
| 6. Destinatário suspeito | 1.152 | 0 | 0 | 588 | 564 |
| 7. Remetente suspeito | 1.152 | 0 | 0 | 588 | 564 |
| 8. Novo telefone, depois antigo | 1.152 | 0 | 369 | 555 | 228 |
| 9. Empresa, filial em novo país, conta nova, movimento alto | 144 | 1.008 | 0 | 132 | 12 |
| 10. Mesma titularidade | 576 | 576 | 108 | 264 | 204 |
| 11. Empresa pagando funcionários em lote | 144 | 1.008 | 0 | 90 | 54 |
| **Total** | **10.080** | **2.592** | **2.310** | **4.920** | **2.850** |

As restrições de aplicabilidade são: caso 9 exige origem PJ, origem recente e
múltiplas tentativas; caso 10 exige a mesma natureza para origem e destino; caso
11 exige PJ → PF e múltiplas tentativas. Mesmo as linhas inaplicáveis retêm todas
as nove escolhas de fatores no CSV.

## Leitura dos resultados

- Os casos 1 e 2 tiveram decisões idênticas ao caso 5 em todas as 1.152
  combinações: o contexto não contém um sinal de roubo ou coação.
- O caso 3 também foi idêntico ao caso 5: a política reconhece destinatário
  novo e velocidade, mas não possui critério separado para diversidade de
  contas de destino.
- No caso 4, `MULE_FAN_IN` apareceu em 192 linhas; em 96 delas o status foi
  mais restritivo que no caso 5 com os mesmos fatores. A etiqueta “legítimo”
  não é um fato observável pelo motor.
- Nos casos 6 e 7, a evidência de reputação acrescentou a razão adversa em
  todas as linhas; nenhuma terminou `approved` sob esta premissa de cache válido.
- O caso 8 foi idêntico ao caso 5 porque o aparelho antigo ainda estava
  `registered`. Um teste adicional confirmou que um registro bancário
  `revoked` para esse aparelho impõe `rejected` por `DEVICE_REVOKED`.
- Os casos 9 e 11 tiveram os mesmos resultados do caso 5 nas combinações
  correspondentes. A política não recebe país da filial nem marca uma folha de
  pagamentos como legítima; seus efeitos vêm de natureza PJ, histórico, valor,
  horário e velocidade.
- Desconhecida, habitual e distante produziram sempre o mesmo resultado para
  os outros oito fatores fixos. A API aceita coordenadas, mas `to_pix` não as
  projeta no contexto do motor.

Estes resultados mostram **decisões determinísticas da política sob premissas
sintéticas**. Não medem taxa de fraude, falsos positivos reais, precisão de
geolocalização nem o fluxo HTTP/PostgreSQL, que não foi executado nesta matriz.
