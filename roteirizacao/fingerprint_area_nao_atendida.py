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

Chave: o CÓDIGO do pedido (26/11, pool direto da Stokki): o pedido nasce
com id sintético negativo e ganha o id da Vuupt só no envio -- pelo
service_id o aviso repetiria quando o id troca. O service_id continua
gravado e consultado (registros antigos não têm código).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _codigo(codigo) -> str | None:
    """Mesma regra de nucleo.normalizacao.normalizar_codigo ('#ps-1 ' -> 'PS-1')."""
    from nucleo.normalizacao import normalizar_codigo
    return normalizar_codigo(codigo)


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_area_notificada (
            service_id      INTEGER PRIMARY KEY,
            tipo            TEXT NOT NULL,
            notificado_em   TEXT NOT NULL,
            codigo          TEXT
        )
    """)
    if "codigo" not in {r[1] for r in conn.execute("PRAGMA table_info(pedidos_area_notificada)")}:
        try:
            conn.execute("ALTER TABLE pedidos_area_notificada ADD COLUMN codigo TEXT")   # tabela de antes de 26/11
        except sqlite3.OperationalError as e:
            # corrida logo apos o deploy: outro processo fez o ALTER entre o PRAGMA e aqui
            if "duplicate column name" not in str(e):
                raise
    conn.commit()
    return conn


def ja_notificado(service_id: int, codigo: str | None = None) -> bool:
    """True se este pedido já foi notificado sobre área não atendida
    (de qualquer tipo -- sp_nao_atendido ou fora_sp), pelo service_id OU
    pelo código (o id do pedido troca quando ele ganha o serviço na Vuupt)."""
    codigo = _codigo(codigo)
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM pedidos_area_notificada WHERE service_id = ? OR (? IS NOT NULL AND codigo = ?)",
        (service_id, codigo, codigo)
    ).fetchone()
    conn.close()
    return row is not None


def marcar_notificado(service_id: int, tipo: str, codigo: str | None = None):
    """Registra que este pedido foi notificado. `tipo`: 'sp_nao_atendido'
    ou 'fora_sp'."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO pedidos_area_notificada (service_id, tipo, notificado_em, codigo)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(service_id) DO UPDATE SET
            tipo = excluded.tipo,
            notificado_em = excluded.notificado_em,
            codigo = COALESCE(excluded.codigo, pedidos_area_notificada.codigo)
    """, (service_id, tipo, agora, _codigo(codigo)))
    conn.commit()
    conn.close()
