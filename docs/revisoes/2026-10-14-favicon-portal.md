# Revisao de seguranca: favicon no portal (2026-10-14)

Diff: origin/master 77c7797 -> favicon-portal (b93ceb1 + merge), modulo portal_cliente/.

- 3 templates (base_acesso, base_cliente, regioes): +3 tags <link rel="icon"/"apple-touch-icon"> com url_for('static', ...) e nome de arquivo fixo. Nenhum dado do usuario, nenhuma rota nova, nenhum script.
- 3 binarios novos em portal_cliente/static/ (favicon.ico, favicon.png, apple-touch-icon.png), copias byte a byte de painel_agentes/static/. Servidos pela rota static padrao do Flask, que ja existia (atendimento.js).

Achados: nenhum ALTO, nenhum MEDIO, nenhum BAIXO.
