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

Como o VUUPT nunca volta um serviço pra 'not_assigned' depois que ele
sai desse status, uma vez confirmado isso pode ser cacheado PRA SEMPRE
localmente — nem precisa consultar o VUUPT de novo nas próximas
execuções pra esse mesmo pedido.
"""
import logging
import sqlite3
from datetime import datetime
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


def ja_confirmado_atribuido(codigo_ps: str) -> bool:
    """
    True se já confirmamos ANTES que este pedido saiu de 'not_assigned'
    no VUUPT — seguro pra pular por completo (nem geocodificar, nem
    resolver endereço), sem precisar consultar o VUUPT de novo.
    """
    codigo = _normalizar_codigo(codigo_ps)
    if not codigo:
        return False
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM pedidos_atribuidos_vuupt WHERE codigo_ps = ?", (codigo,)
    ).fetchone()
    conn.close()
    return row is not None


def marcar_atribuido(codigo_ps: str, status: str = ""):
    """Registra que este pedido já saiu de 'not_assigned' no VUUPT —
    fica marcado pra sempre, nunca mais precisa checar de novo."""
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
