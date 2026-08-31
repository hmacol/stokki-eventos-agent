#!/usr/bin/env bash
# sequencia_noite.sh -- equivalente ao rodar_sequencial_noite.ps1
# (StokkiEventos_SequenciaNoite, 22:00). incrementar_rotas.py FALHA DE
# PROPOSITO se o rascunho do dia nao foi confirmado em /planejamento --
# comportamento esperado, nao e bug (ver DOC_EXECUCAO_CLAUDE_MIGRACAO_VPS.md).
#
# enviar_rascunhos_pendentes.py revivido em 23/08 (Fase 3 do roadmap de
# roteirizacao, portao automatico): roda ANTES do incrementar_rotas.py
# de proposito -- manda pra VUUPT so o rascunho pendente aprovado na
# nota de qualidade, o resto fica em RASCUNHO e o incrementar_rotas.py
# continua falhando de proposito pra esses (mesma rede de seguranca de
# antes). Nunca sai com erro por causa de rascunho reprovado -- so loga
# e notifica por e-mail -- entao nao quebra o "set -e" da sequencia.
RAIZ="/opt/stokki-eventos"
PY="$RAIZ/venv/bin/python"

# 31/08 (pedido do Hugo): --sem-impressao SO AQUI na sequencia da noite --
# a etapa da Estacao de Impressao (Em espera -> Aguardando Transportador)
# continua rodando as 18h (sequencia_tarde.sh) e as 08:20/15:20
# (notificar_pedidos_em_espera.py); pedidos faturados a noite ficam em
# espera ate a rodada das 08:20 do dia seguinte.
cd "$RAIZ" && "$PY" executar_tudo.py --sem-impressao
cd "$RAIZ" && "$PY" verificar_pedidos_duplicados_vuupt.py
cd "$RAIZ/roteirizacao" && "$PY" enviar_rascunhos_pendentes.py
cd "$RAIZ/roteirizacao" && "$PY" incrementar_rotas.py
