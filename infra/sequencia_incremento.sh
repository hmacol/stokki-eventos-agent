#!/usr/bin/env bash
# sequencia_incremento.sh -- rodada das 18:00 na VPS
# (stokki-sequencia-incremento.timer). 05/10 (Hugo): o rascunho passou
# pras 16h (sequencia_tarde.sh) e as 18h roda so o incremento: importa o
# que chegou, e incrementar_rotas.py poe cada pedido novo na rota ja
# enviada ou no rascunho ainda nao confirmado mais proximo. O mesmo
# incremento roda de novo no fim da sequencia das 22h.
# set -e como a da tarde: se a importacao ou a checagem de duplicados
# falhar, nao incrementa com dado pela metade.
set -e

RAIZ="/opt/stokki-eventos"
PY="$RAIZ/venv/bin/python"

cd "$RAIZ" && "$PY" executar_tudo.py
cd "$RAIZ" && "$PY" verificar_pedidos_duplicados_vuupt.py
cd "$RAIZ" && "$PY" atualizar_agendamentos_confirmados.py
cd "$RAIZ" && "$PY" marcar_clientes_agenda.py || true
cd "$RAIZ/roteirizacao" && "$PY" incrementar_rotas.py
