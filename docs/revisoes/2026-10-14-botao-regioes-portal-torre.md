# Review de segurança: botão "Dias de atendimento" no portal e na Torre

Data: 14/10/2026. Ramo botao-regioes → master. Base em produção: c4d52ee.

## Escopo

- `portal_cliente/templates/base_cliente.html` e `login.html`: um link `<a>` para a rota
  pública `/regioes` (via `url_for`), `target=_blank` com `rel=noopener`.
- `portal_cliente/templates/_tour_portal.html`: um passo do guia apontando pro botão.
- `painel_agentes/painel_agentes.py`: constante `URL_PAGINA_REGIOES` importada de
  `portal_cliente.regioes_atendimento.URL_PUBLICA` e passada às duas rotas da Torre.
- `painel_agentes/templates/torre_controle.html` e `torre_mobile.html`: o mesmo link.

## Análise

- Nenhuma rota nova, nenhum parâmetro do usuário, nenhum dado de cliente.
- O destino é a página pública já revisada em 07/10 (sem dados de pedidos).
- `rel=noopener` em todos os `target=_blank`.
- Cobertura: `test_regioes_pagina.test_login_tem_link_direto_pra_pagina` (link com o
  prefixo /cliente). Torre: templates parseados; prova na VPS por renderização.

## Achados

Nenhum.
