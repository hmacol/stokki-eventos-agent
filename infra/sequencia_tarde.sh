#!/usr/bin/env bash
# sequencia_tarde.sh -- equivalente ao rodar_sequencial.ps1, pra rodar via
# systemd timer na VPS (StokkiEventos_SequenciaTarde, 18:00 local ->
# stokki-eventos-sequencia-tarde.timer na VPS). Cada passo so comeca depois
# que o anterior termina de verdade (set -e -- para a sequencia inteira se
# um passo falhar, mesmo comportamento do -Wait do PowerShell original).
set -e

RAIZ="/opt/stokki-eventos"
PY="$RAIZ/venv/bin/python"

cd "$RAIZ" && "$PY" executar_tudo.py
cd "$RAIZ" && "$PY" verificar_pedidos_duplicados_vuupt.py
# 28/09: data de agendamento informada (portal/planilha/e-mail) entra no
# servico ANTES da roteirizacao -- senao o dia fixo preenche a data dele.
cd "$RAIZ" && "$PY" atualizar_agendamentos_confirmados.py
# 29/09: quem teve data de agendamento informada vira AGENDA na BD_CLIENTES.
# Falha aqui nao pode parar a roteirizacao (|| true por causa do set -e).
cd "$RAIZ" && "$PY" marcar_clientes_agenda.py || true
cd "$RAIZ/roteirizacao" && "$PY" criar_rotas_diarias.py --gerar-rascunho
# 28/08: notificar_pedidos_em_espera.py saiu daqui -- roda em timer proprio
# (stokki-notificar-pedidos-em-espera.timer, 08:20 e 15:20).
cd "$RAIZ/documentos_pedido" && "$PY" processar_documentos.py
