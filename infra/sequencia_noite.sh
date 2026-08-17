#!/usr/bin/env bash
# sequencia_noite.sh -- equivalente ao rodar_sequencial_noite.ps1
# (StokkiEventos_SequenciaNoite, 22:00). incrementar_rotas.py FALHA DE
# PROPOSITO se o rascunho do dia nao foi confirmado em /planejamento --
# comportamento esperado, nao e bug (ver DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md).
set -e

RAIZ="/opt/stokki-eventos"
PY="$RAIZ/venv/bin/python"

cd "$RAIZ" && "$PY" executar_tudo.py
cd "$RAIZ" && "$PY" verificar_pedidos_duplicados_vuupt.py
cd "$RAIZ/roteirizacao" && "$PY" incrementar_rotas.py
