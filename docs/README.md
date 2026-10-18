# Documentação

O [README](../README.md) é o documento principal: regras, composição da decisão,
como rodar, erros, logs e testes. Os arquivos abaixo aprofundam cada parte.

| Documento | Conteúdo |
|---|---|
| [api-pix.md](api-pix.md) | Contrato HTTP, configuração por tenant, protocolo do coordenador, deadline, falhas e logs |
| [motor-pix.md](motor-pix.md) | Motor puro: contexto imutável, features, critérios, política versionada e auditoria |
| [providers-pix.md](providers-pix.md) | Contratos dos providers simulados, seleção de versões, cache e enriquecimento |
| [storage-pix.md](storage-pix.md) | Persistência: claims, leases, locks, eventos bancários e snapshots |
| [migrations.md](migrations.md) | Migrations SQL versionadas, adoção de banco existente e testes de upgrade |
| [aceitacao-pix.md](aceitacao-pix.md) | Matriz de aceitação do guia V3 §11.2, demonstração reproduzível e limites da evidência |
| [guia-apresentacao.md](guia-apresentacao.md) | Roteiro da apresentação (também em [PDF](guia-apresentacao.pdf)) |
| [referencia/](referencia/) | Guia V3 de implementação e a referência Python V3 (motor, contrato, testes e resultados) usada pela comparação `tests/reference/verify_pix_reference.py` |

A política é experimental; nenhum documento alega calibração, eficácia antifraude ou
conformidade regulatória. Decisões de desenho seguem o guia V3; pesos, cortes e janelas
são hipóteses reproduzíveis para a demonstração.
