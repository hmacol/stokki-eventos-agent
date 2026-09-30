# -*- coding: utf-8 -*-
"""
fingerprint_rotas.py

Registro local de quais pedidos já foram adicionados a uma rota pelo
incrementar_rotas.py -- garante que não tentamos adicionar o mesmo
pedido duas vezes, independente de o VUUPT mudar (ou não) o status do
serviço quando ele entra numa rota (ainda não confirmado empiricamente
-- ver nota em incrementar_rotas.py). Mesmo padrão de fingerprint já
usado no resto do projeto (fingerprint_expedicao.py, fingerprint_
status_vuupt.py).
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
        CREATE TABLE IF NOT EXISTS pedidos_roteirizados (
            service_id      INTEGER PRIMARY KEY,
            route_id        INTEGER NOT NULL,
            alocado_em      TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def marcar_alocado(service_id: int, route_id: int):
    """Registra que este pedido foi adicionado à rota `route_id`."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO pedidos_roteirizados (service_id, route_id, alocado_em)
        VALUES (?, ?, ?)
        ON CONFLICT(service_id) DO UPDATE SET
            route_id = excluded.route_id,
            alocado_em = excluded.alocado_em
    """, (service_id, route_id, agora))
    conn.commit()
    conn.close()
