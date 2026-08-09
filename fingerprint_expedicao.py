# -*- coding: utf-8 -*-
"""
fingerprint_expedicao.py

Registro local de pedidos JÁ expedidos na Stokki com o canhoto tratado
(anexado com sucesso, ou confirmado que já estava anexado) — pra não
reprocessar o mesmo pedido em toda execução do expedir_pedidos.py
(pedido do Hugo, 30/07: "estamos sempre reprocessando diversas vezes
os mesmos pedidos").

Espelha o espírito de fingerprint_importacao.py (lado da importação),
mas aqui do lado da expedição: uma tabela local (dados/dados.db) que
guarda quais códigos já foram tratados por completo.

IMPORTANTE — só marca como processado quando há CERTEZA de que está
completo (expedido + canhoto anexado/confirmado). Falha na expedição,
falha ao anexar, ou ausência de PDF (pode ser algo transitório do
VUUPT) NUNCA são marcados — o pedido continua elegível pra tentar de
novo na próxima execução. Isso evita "esconder" um pedido que na
verdade ainda precisa de atenção.
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expedicoes_processadas (
            codigo_ps        TEXT PRIMARY KEY,
            vuupt_service_id INTEGER,
            canhoto_anexado  INTEGER NOT NULL DEFAULT 0,
            processado_em    TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    return conn


def _normalizar(codigo_ps: str) -> str:
    return (codigo_ps or "").strip().lstrip("#").upper()


def ja_processado(codigo_ps: str) -> bool:
    """
    True se este pedido já foi expedido + canhoto tratado com sucesso
    numa execução anterior — pode ser pulado sem precisar consultar o
    VUUPT/Stokki de novo.
    """
    codigo = _normalizar(codigo_ps)
    if not codigo:
        return False
    try:
        conn = _conectar()
        row = conn.execute(
            "SELECT 1 FROM expedicoes_processadas WHERE codigo_ps = ?", (codigo,)
        ).fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.debug(f"Erro ao checar fingerprint de expedição para {codigo_ps}: {e}")
        return False  # em dúvida, NÃO pula -- mais seguro tentar de novo


def marcar_processado(codigo_ps: str, vuupt_service_id=None, canhoto_anexado: bool = True):
    """Registra que este pedido foi expedido e o canhoto foi tratado com sucesso."""
    codigo = _normalizar(codigo_ps)
    if not codigo:
        return
    try:
        conn = _conectar()
        conn.execute("""
            INSERT INTO expedicoes_processadas
                (codigo_ps, vuupt_service_id, canhoto_anexado, processado_em)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(codigo_ps) DO UPDATE SET
                vuupt_service_id = excluded.vuupt_service_id,
                canhoto_anexado  = excluded.canhoto_anexado,
                processado_em    = excluded.processado_em
        """, (codigo, vuupt_service_id, int(canhoto_anexado),
              datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Erro ao marcar fingerprint de expedição para {codigo_ps}: {e}")
