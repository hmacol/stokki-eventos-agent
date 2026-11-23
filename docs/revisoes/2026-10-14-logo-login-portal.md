# Review de segurança: logo nas telas de acesso do portal (14/10/2026)

Escopo: `git diff a062430 logo-login-portal -- portal_cliente/templates/base_acesso.html` (5+/6-).

## Mudança
- Troca o selo "FL" + texto "Fresh Log" por `<img src="{{ url_for('regioes_logo') }}">`.
- CSS de `.marca` ajustado.

## Análise
- Nenhuma entrada do usuário passa pelo trecho alterado; o `src` é gerado por `url_for` com endpoint fixo.
- `regioes_logo` já é rota pública (sem `requer_cliente`), serve arquivo fixo (`assets/logo_freshlog.png`) via `send_file`, sem parâmetro de caminho: nada de path traversal.
- `regioes_logo` está em `_LIVRES_ANTES_DA_TROCA`, então a tela de troca obrigatória de PIN carrega o logo sem redirect em loop.
- Nenhuma rota, sessão, autenticação ou cabeçalho alterado. Sem recurso externo novo (CSP/terceiros inalterados).

## Achados
Nenhum ALTO, MÉDIO ou BAIXO.
