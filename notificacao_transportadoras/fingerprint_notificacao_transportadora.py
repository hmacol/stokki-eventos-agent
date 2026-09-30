# -*- coding: utf-8 -*-
"""
fingerprint_notificacao_transportadora.py

Controle de envio da notificação de transportadora com XML da NF-e (ver
notificar_transportadoras.py) -- mesmo padrão de redespacho_confirmacao.py:
SQLite em dados/dados.db, 1 registro por (pedido, transportadora). Rate-limit
é incremental (pedido do Hugo, 13/08): um pedido já notificado hoje pra uma
transportadora não entra de novo no mesmo dia, mas um pedido NOVO que surja
mais tarde pra mesma transportadora gera um e-mail extra só com ele.
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
        CREATE TABLE IF NOT EXISTS notificacoes_transportadora_enviadas (
            pedido           TEXT NOT NULL,
            transportadora   TEXT NOT NULL,
            enviado_em       TEXT NOT NULL,
            PRIMARY KEY (pedido, transportadora)
        )
    """)
    conn.commit()
    return conn


def pedidos_ja_notificados_hoje(transportadora: str) -> set[str]:
    """Pedidos já notificados HOJE pra essa transportadora (nome_normalizado
    do catálogo -- ver notificar_transportadoras.py). Notificações de dias
    anteriores não contam -- se o pedido ainda estiver na rota de outro dia,
    é uma entrega diferente."""
    conn = _conectar()
    hoje = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT pedido FROM notificacoes_transportadora_enviadas "
        "WHERE transportadora = ? AND substr(enviado_em, 1, 10) = ?",
        (transportadora, hoje),
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def registrar_notificacao(pedido: str, transportadora: str):
    conn = _conectar()
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO notificacoes_transportadora_enviadas (pedido, transportadora, enviado_em)
        VALUES (?, ?, ?)
        ON CONFLICT(pedido, transportadora) DO UPDATE SET
            enviado_em = excluded.enviado_em
    """, (pedido, transportadora, agora))
    conn.commit()
    conn.close()
