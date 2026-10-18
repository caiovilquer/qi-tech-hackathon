# Evidências históricas da revisão V2

Esta pasta guarda o diagnóstico dos anexos originalmente recebidos. As cópias em `originais/` foram preservadas antes da correção. `verificar.py` usa essas cópias e o ZIP original na raiz para reproduzir as 16 observações anteriores.

O sucesso desse script significa que os problemas antigos continuam reproduzíveis nos originais. **Não é o teste de aceitação do motor corrigido.** Os testes atuais estão em `test_contrato_pix.py` e `test_politica_pix.py`, na raiz; o documento vigente é [GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md](../GUIA-IMPLEMENTACAO-ANTIFRAUDE-PIX.md).

```bash
tmp/revisao-v2/.venv/bin/python revisao-v2/verificar.py
```
