#!/usr/bin/env bash
# sequencia_noite.sh -- rodada das 22:00 na VPS (stokki-sequencia-noite.timer).
#
# 09/09 (pedido do Hugo, a noite): a sequencia da noite DEIXOU de
# roteirizar. Antes rodava executar_tudo.py --sem-impressao +
# verificar_pedidos_duplicados_vuupt.py + enviar_rascunhos_pendentes.py +
# incrementar_rotas.py. Agora roda SO estas cinco etapas, nesta ordem:
#
#   1. Impressao de pedidos  -- Estacao de Impressao (Em espera ->
#                               Aguardando Transportador). Volta a rodar
#                               a noite (o --sem-impressao de 31/08 caiu).
#   2. Importacao de pedidos -- pipeline.py (Stokki -> VUUPT), sem filtro.
#   3. Expedicao de pedidos  -- expedir_pedidos.py (entregues expedem na
#                               Stokki; insucessos tratados a parte). O
#                               e-mail de "dia limpo" tem trava de 1/dia,
#                               entao nao repete o das 19h30.
#   4. Documentacoes         -- processar_documentos.py (NF/boleto/CC/
#                               agendamento -> GCS; NF no portal do cliente).
#   5. Geracao de PDFs       -- gerar_pdf_romaneios.py --data amanha
#                               (romaneios das rotas do dia seguinte; o
#                               job das 04h continua regenerando "hoje").
#
# Sem set -e de proposito: cada etapa notifica sozinha por e-mail
# (notificar_execucao) e uma falha nao deve impedir as seguintes --
# em especial documentos e PDFs, que sao o motivo de rodar a noite.
RAIZ="/opt/stokki-eventos"
PY="$RAIZ/venv/bin/python"

cd "$RAIZ" && "$PY" somente_impressao.py
cd "$RAIZ" && "$PY" pipeline.py
cd "$RAIZ" && "$PY" expedir_pedidos.py
cd "$RAIZ/documentos_pedido" && "$PY" processar_documentos.py
cd "$RAIZ/roteirizacao" && "$PY" gerar_pdf_romaneios.py --data amanha
