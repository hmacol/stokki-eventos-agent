# Review de segurança: informativo mensal e lembrete de virada de mês

Data: 07/10/2026. Ramo informativo-mensal → master. Base em produção: 8d65800.

## Escopo nos módulos expostos

- `portal_cliente/app.py`: a rota pública `/regioes` passa a entregar os links ao
  template por variáveis (`logo_src`, `url_mes_anterior`, `url_mes_seguinte`,
  `url_portal`), todos vindos de `url_for`. Nenhum parâmetro novo, nenhuma rota nova.
- `portal_cliente/regioes_atendimento.py`: funções puras novas (`virada_de_mes`,
  `feriados_uteis_do_mes`, `url_do_mes`) e duas constantes de URL. Sem I/O.
- `portal_cliente/templates/regioes.html`: só troca `url_for` por variáveis; autoescape
  do Jinja continua valendo. Os valores são URLs geradas pelo servidor.
- `painel_agentes/pagina_inicial.py` + `inicio.html`: bloco "Virada de mês" montado da
  configuração (sem dados de clientes), atrás de `requer_auth`. O JS cria elementos via
  `textContent`, sem `innerHTML`; o link abre a página pública em nova aba com `noopener`.

## Script e timer (não expostos)

- `gerar_informativo_mensal.py` roda como www-data pelo timer, lê config.yaml, escreve em
  dados/informativos/, envia e-mail (destino redirecionado ao Hugo por padrão) e um
  WhatsApp pelo canal interno existente (teto diário, assinatura `informativo:<mês>`).
  `--mes` aceita só `AAAA-MM` (int + date); entrada ruim levanta ValueError e o
  OnFailure avisa. Playwright só renderiza HTML gerado pelo próprio sistema (`set_content`),
  sem navegar a URL externa.

## Achados

Nenhum ALTO, MÉDIO ou BAIXO.
