# Review de segurança: página pública /regioes no portal do cliente

Data: 07/10/2026. Ramo regioes-portal → master. Base em produção: e822848.

## Escopo

`portal_cliente/app.py` ganha duas rotas públicas (sem sessão):

- `GET /regioes` → `render_template("regioes.html", d=regioes_atendimento.montar(...))`
- `GET /regioes/logo.png` → `send_file(assets/logo_freshlog.png)` (caminho fixo)

`portal_cliente/regioes_atendimento.py` é puro: lê `roteirizacao/regioes_dia_fixo.py`
e `regras/feriados.py`; sem banco, sem Vuupt, sem config.

## Análise

- **Entrada do usuário:** só `?mes=AAAA-MM`. `validar_mes` faz `int()` nas duas partes,
  valida `date(ano, mes, 1)` e limita à janela (1 mês atrás, 3 à frente); qualquer
  falha cai no mês corrente. Nenhum valor da query chega ao template sem passar por aí.
- **Template:** Jinja com autoescape (Flask, `.html`). Todo texto vem da configuração
  do repositório (nomes de regiões, cidades, endereços de galpões), não de dados de
  clientes. Nada de dados de pedidos ou embarcadores.
- **Dados expostos:** regiões, dias de visita, endereços de 4 galpões/operadores
  (destinos de entrega, já públicos) e e-mail entregas@. É o conteúdo do informativo
  que o Hugo pediu pra publicar.
- **send_file:** caminho literal dentro do repositório; sem parâmetro do usuário.
- **Cache:** `Cache-Control: public, max-age=900` só nessa rota; não há cookie de sessão
  em jogo (a rota não usa `g.cliente`). O `before_request` continua rodando, mas
  `_LIVRES_ANTES_DA_TROCA` inclui as duas rotas, então um cliente com troca de PIN
  pendente não é redirecionado nem bloqueia a página.
- **DoS:** `montar` percorre ~31 dias × 12 regras; custo desprezível, e o cache de 15 min
  no navegador/Caddy reduz repetição.
- **Cobertura:** `test_regioes_atendimento.py` (9 testes: dados, feriado, janela de mês)
  e `test_regioes_pagina.py` (4 testes: 200 sem login, lixo na query, logo, prefixo /cliente).

## Achados

Nenhum ALTO, MÉDIO ou BAIXO.
