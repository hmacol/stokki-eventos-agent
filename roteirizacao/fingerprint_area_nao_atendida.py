# -*- coding: utf-8 -*-
"""
fingerprint_area_nao_atendida.py

Registro local de pedidos já notificados sobre área fora de
atendimento (ver notificar_area_nao_atendida.py) -- diferente do
agendamento (que é rastreado por dia e pode reenviar), aqui o envio é
ÚNICO: assim que o remetente é avisado 1 vez que a região não é
atendida (ou que precisa confirmar redespacho), não faz sentido
reenviar isso todo dia -- é uma decisão que cabe ao remetente resolver
por fora (orçamento, redespacho, etc.), não uma resposta estruturada
que o sistema vai aplicar sozinho depois.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_area_notificada (
            service_id      INTEGER PRIMARY KEY,
            tipo            TEXT NOT NULL,
            notificado_em   TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def ja_notificado(service_id: int) -> bool:
    """True se este pedido já foi notificado sobre área não atendida
    (de qualquer tipo -- sp_nao_atendido ou fora_sp)."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM pedidos_area_notificada WHERE service_id = ?", (service_id,)
    ).fetchone()
    conn.close()
    return row is not None


def marcar_notificado(service_id: int, tipo: str):
    """Registra que este pedido foi notificado. `tipo`: 'sp_nao_atendido'
    ou 'fora_sp'."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO pedidos_area_notificada (service_id, tipo, notificado_em)
        VALUES (?, ?, ?)
        ON CONFLICT(service_id) DO UPDATE SET
            tipo = excluded.tipo,
            notificado_em = excluded.notificado_em
    """, (service_id, tipo, agora))
    conn.commit()
    conn.close()
