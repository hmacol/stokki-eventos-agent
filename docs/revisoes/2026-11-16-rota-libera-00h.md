# Revisao de seguranca: rota libera as 00h (commit 4a7703c, ramo rota-libera-00h)

Escopo: `git diff aa5cd0b 4a7703c -- nucleo/operacao.py` (modulo exposto pela API do motorista).

O que muda: `rota_liberada` (data_rota <= hoje no fuso da VPS); `montar_rota` devolve so
id/data/start_at/status para rota de dia futuro; aceitar/recusar/iniciar/evento de parada
respondem 409 antes da liberacao. A mudanca so RESTRINGE: nenhum dado novo sai, nenhuma
permissao nova. A checagem de dono (`rota_do_motorista`) continua antes da de liberacao,
entao rota de outro motorista segue 404 (nao revela existencia).

ALTO: nenhum. MEDIO: nenhum.

BAIXO
- B1. `/api/financeiro` nao passa por `rota_liberada`: se aceitar periodo futuro, pode mostrar
  valor/contagem de rota ainda trancada. Nao conferido a fundo; a aba esta oculta no app.
- B2. Comprovante, pedagio e GPS nao checam liberacao. Exigem id de parada (que a rota trancada
  nao entrega) ou rota EM_ROTA (inalcancavel antes das 00h). Sem caminho pratico.
- B3. A trava usa o relogio da VPS (America/Sao_Paulo); o app nao decide nada, so le `liberada`.

Testes: nucleo.test_api_motorista (novo test_rota_de_amanha_so_avisa_e_libera_a_meia_noite),
test_ponte_vuupt, test_cadastro_motorista, test_validacao_fotos: 76 OK.
