# Gabarito proposto e comparação da matriz Pix

Este gabarito é **uma expectativa de negócio para os cenários sintéticos**, não
uma especificação oficial nem um cálculo reproduzido das regras do motor. Ele
usa a descrição de cada caso como verdade conhecida por quem avalia o teste.
O motor só vê os campos do `Context`; por isso um desvio aponta uma lacuna de
detecção ou uma decisão conservadora **sob estas premissas**, não prova uma
taxa de falsos positivos ou uma falha em produção.

## Critérios do gabarito

| Caso | Status aceito | Premissa |
|---|---|---|
| 1. Aparelho roubado | `rejected` | Roubo confirmado; por prudência, 2FA sem garantia de canal independente não substitui bloqueio. |
| 2. Coação | `rejected` | Coação confirmada; o usuário pode completar um desafio sob ameaça. |
| 3. Envio legítimo a contas diferentes | `approved` ou `two_factor_required` | Operação legítima; autenticação adicional é aceitável. |
| 4. Recebimento legítimo de contas diferentes | `approved` ou `two_factor_required` | Operação legítima; autenticação adicional é aceitável. |
| 5. Pix genérico | Indeterminado | O enunciado não diz se é legítimo ou fraudulento. |
| 6. Destinatário suspeito | `two_factor_required` ou `rejected` | Um relato aberto válido é evidência de suspeita, não confirmação de fraude. |
| 7. Remetente suspeito | `two_factor_required` ou `rejected` | Mesma premissa para o documento do remetente. |
| 8. Novo telefone, depois antigo | Igual ao caso 5 com os mesmos nove fatores | O banco ainda informa que o dispositivo antigo está `registered`. Esta é uma verificação de consistência, não um juízo de segurança sobre a troca. |
| 9. Nova filial e alta movimentação | `approved` ou `two_factor_required` | A atividade da filial é legítima, mesmo sendo atípica. |
| 10. Mesma titularidade | `approved` ou `two_factor_required` | Titularidade igual e transferência legítima. |
| 11. Folha de pagamento | `approved` ou `two_factor_required` | Pagamentos legítimos de folha. |

Para os casos 3, 4, 9, 10 e 11, `rejected` é **divergente deste gabarito de
experiência desejada**, mesmo quando decorre da pontuação da política atual.
Um limite bancário explícito poderia justificar um bloqueio de operação
legítima; nenhum caso da matriz forneceu tal restrição. Nos casos 1 e 2,
`two_factor_required` foi classificado como divergente porque a confirmação
do desafio não afasta o roubo ou a coação conhecidos no enunciado.

## Comparação de todas as permutações

| Caso | Avaliadas | Conformes | Divergentes | Indeterminadas | Inaplicáveis |
|---|---:|---:|---:|---:|---:|
| 1. Aparelho roubado | 1.152 | 228 | 924 | 0 | 0 |
| 2. Coação | 1.152 | 228 | 924 | 0 | 0 |
| 3. Envio legítimo | 1.152 | 924 | 228 | 0 | 0 |
| 4. Recebimento legítimo | 1.152 | 840 | 312 | 0 | 0 |
| 5. Pix genérico | 1.152 | 0 | 0 | 1.152 | 0 |
| 6. Destinatário suspeito | 1.152 | 1.152 | 0 | 0 | 0 |
| 7. Remetente suspeito | 1.152 | 1.152 | 0 | 0 | 0 |
| 8. Novo telefone, depois antigo | 1.152 | 1.152 | 0 | 0 | 0 |
| 9. Filial em outro país | 144 | 132 | 12 | 0 | 1.008 |
| 10. Mesma titularidade | 576 | 372 | 204 | 0 | 576 |
| 11. Folha de pagamento | 144 | 90 | 54 | 0 | 1.008 |
| **Total** | **10.080** | **6.270** | **2.658** | **1.152** | **2.592** |

As 2.592 linhas inaplicáveis continuam presentes no CSV: no caso 9 a origem
precisa ser PJ e recente, com múltiplas tentativas; no 10, as naturezas de
origem e destino precisam coincidir; no 11, precisa haver PJ → PF e múltiplas
tentativas.

## O que os desvios mostram

- **Roubo e coação:** em cada caso, 369 combinações foram `approved`, 555
  exigiram 2FA e 228 foram `rejected`. Os 228 bloqueios vieram de sinais
  comportamentais, pois o contexto não recebe um sinal de roubo ou coação.
  Os dois casos reproduziram integralmente o caso 5. Logo, os 924 desvios por
  caso mostram falta de observabilidade para esses fatos, não uma falha
  comprovada de uma regra que os receba.
- **Operações legítimas:** houve 810 rejeições nos casos 3, 4, 9, 10 e 11.
  Todas alcançaram `score >= 70`; nenhuma foi causada por `NIGHT_HARD_LIMIT`,
  restrição bancária ou limite bancário explícito nesta matriz. São bloqueios
  por combinação de sinais como rajada de tentativas, conta de destino recente,
  valor acima do perfil e fan-in. É uma oportunidade de calibrar a política
  com dados reais, sem assumir que os 810 casos ocorram com essa frequência.
- **Suspeita documentada:** nos casos 6 e 7, nenhuma das 2.304 análises foi
  aprovada sob a premissa de um relato aberto válido em cache. Todas ficaram
  em 2FA ou rejeição, conforme o mínimo estabelecido neste gabarito.
- **Troca de telefone:** o caso 8 coincidiu com o caso 5 nas 1.152 linhas.
  Se o banco revogar o aparelho antigo, `DEVICE_REVOKED` passa a impor
  `rejected`; a matriz principal não simula essa revogação.
- **Localização:** as três variantes de localização deram o mesmo resultado
  para cada combinação dos outros fatores. O gerador registra as coordenadas,
  mas `to_pix` não as transporta para o `Pix` do motor. Portanto, o gabarito
  não presume que distância geográfica sozinha prove fraude.

## Arquivos e reprodução

- [`pix_permutation_gabarito.csv`](pix_permutation_gabarito.csv): 12.672 linhas
  com fatores, decisão observada, tipo de gabarito, status aceitos, comparação
  e justificativa. `*.csv` é ignorado pelo Git neste repositório.
- [`pix_permutation_comparison.json`](pix_permutation_comparison.json): totais
  por caso e por classificação.
- [`pix_permutation_matrix.csv`](pix_permutation_matrix.csv): decisões originais
  usadas como entrada.

```bash
PYTHONPATH=src:. python3 -m tests.support.pix_permutation_matrix --output-dir reports
PYTHONPATH=src:. python3 -m tests.support.pix_permutation_answer_key --input reports/pix_permutation_matrix.csv --output-dir reports
```

O comparador valida que a entrada contém exatamente as 11 × 1.152 combinações,
sem chave ausente ou duplicada. A definição do gabarito usa apenas o caso e os
fatores; pontuação, motivos e status observados são lidos somente na etapa de
comparação. O caso 8 usa o resultado do caso 5 como referência relacional.
