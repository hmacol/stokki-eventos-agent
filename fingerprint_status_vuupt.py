# -*- coding: utf-8 -*-
"""
fingerprint_status_vuupt.py

Registro local de pedidos que já foram confirmados como FORA do status
'not_assigned' no VUUPT (atribuídos, em andamento, concluídos ou
cancelados) — pedido do Hugo, 31/07: "estamos geocodificando
desnecessariamente diversos pedidos que já estão com status de
importados ou done".

Antes deste módulo, a checagem "esse pedido já saiu de not_assigned?"
só acontecia dentro de criar_ou_atualizar_servico (vuupt_client.py),
BEM no final do processamento — ou seja, DEPOIS de já ter feito toda a
resolução de endereço, geocodificação, telefone, skill e agendamento
pra nada, já que o pedido ia ser descartado de qualquer jeito. A
checagem em si (buscar_servico_por_code) continua existindo lá como
rede de segurança, mas agora processar_pedido faz uma checagem
ANTECIPADA, antes de fazer qualquer trabalho caro.

28/09: a premissa antiga ("o VUUPT nunca volta pra not_assigned") é
FALSA -- cancelar rota com services_action=unassign (cancelar_rotas_sem_
motorista, reprocessar_rotas, "Cancelar rota" do planejamento) devolve
o serviço pro pool. Com o cache eterno o pipeline parava de atualizar
esse pedido (endereço, agendamento confirmado, volumes) pra sempre.
Agora só 'done' fica marcado pra sempre; os outros status valem por
VALIDADE_HORAS e depois o pipeline confere de novo na VUUPT (1 GET).
"""
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _normalizar_codigo(codigo_ps: str) -> str:
    return (codigo_ps or "").strip().lstrip("#").upper()


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_atribuidos_vuupt (
            codigo_ps       TEXT PRIMARY KEY,
            status          TEXT,
            confirmado_em   TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


# Único status que não volta: entrega concluída (sucesso ou insucesso;
# reentrega nasce com outro code, -R1).
STATUS_DEFINITIVOS = {"done"}
VALIDADE_HORAS = 12


def marcacao_vigente(status: str | None, confirmado_em: str | None,
                     agora: datetime | None = None) -> bool:
    """Regra pura: a marcação ainda vale? 'done' vale pra sempre; o
    resto só por VALIDADE_HORAS (assigned/on_route podem voltar a
    not_assigned; canceled precisa aparecer de novo no relatório)."""
    if (status or "") in STATUS_DEFINITIVOS:
        return True
    try:
        quando = datetime.strptime(confirmado_em or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (agora or datetime.now()) - quando < timedelta(hours=VALIDADE_HORAS)


def ja_confirmado_atribuido(codigo_ps: str) -> bool:
    """
    True se já confirmamos que este pedido saiu de 'not_assigned' no
    VUUPT e a marcação ainda vale (marcacao_vigente) — seguro pra pular
    por completo (nem geocodificar, nem resolver endereço).
    """
    codigo = _normalizar_codigo(codigo_ps)
    if not codigo:
        return False
    conn = _conectar()
    row = conn.execute(
        "SELECT status, confirmado_em FROM pedidos_atribuidos_vuupt WHERE codigo_ps = ?", (codigo,)
    ).fetchone()
    conn.close()
    return row is not None and marcacao_vigente(row[0], row[1])


def marcar_atribuido(codigo_ps: str, status: str = ""):
    """Registra que este pedido saiu de 'not_assigned' no VUUPT (vale
    conforme marcacao_vigente)."""
    codigo = _normalizar_codigo(codigo_ps)
    if not codigo:
        return
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO pedidos_atribuidos_vuupt (codigo_ps, status, confirmado_em)
        VALUES (?, ?, ?)
        ON CONFLICT(codigo_ps) DO UPDATE SET
            status = excluded.status,
            confirmado_em = excluded.confirmado_em
    """, (codigo, status, agora))
    conn.commit()
    conn.close()
