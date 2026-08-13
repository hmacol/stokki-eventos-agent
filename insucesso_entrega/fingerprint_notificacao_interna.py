# -*- coding: utf-8 -*-
"""
fingerprint_notificacao_interna.py

Registro local dos insucessos já incluídos no e-mail INTERNO "Pedidos
com Insucesso na Entrega" (pedido do Hugo, 12/08 e 13/08: o e-mail
repetia a janela inteira de 48h a cada 30 min -- deve sair só com os
insucessos do dia e só quando houver insucesso NOVO).

Também controla o e-mail de "dia sem insucesso" (máximo 1 por dia).

Mesmo padrão SQLite dos outros fingerprints (dados/dados.db).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # sobe de insucesso_entrega/ pra raiz do projeto
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insucessos_notificados_interno (
            service_id    INTEGER PRIMARY KEY,
            code          TEXT,
            notificado_em TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS emails_sem_insucesso_dia (
            data       TEXT PRIMARY KEY,
            enviado_em TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def ja_notificado(service_id) -> bool:
    """True se esse insucesso já apareceu em algum e-mail interno."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM insucessos_notificados_interno WHERE service_id = ?",
        (service_id,),
    ).fetchone()
    conn.close()
    return row is not None


def marcar_notificados(servicos: list):
    """Marca todos os serviços da lista como já notificados no e-mail interno."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    for s in servicos:
        conn.execute("""
            INSERT INTO insucessos_notificados_interno (service_id, code, notificado_em)
            VALUES (?, ?, ?)
            ON CONFLICT(service_id) DO NOTHING
        """, (s.get("id"), s.get("code", ""), agora))
    conn.commit()
    conn.close()


def sem_insucesso_ja_enviado(data_iso: str) -> bool:
    """True se o e-mail de 'dia sem insucesso' já saiu nesse dia (YYYY-MM-DD)."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM emails_sem_insucesso_dia WHERE data = ?",
        (data_iso,),
    ).fetchone()
    conn.close()
    return row is not None


def marcar_sem_insucesso_enviado(data_iso: str):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO emails_sem_insucesso_dia (data, enviado_em)
        VALUES (?, ?)
        ON CONFLICT(data) DO NOTHING
    """, (data_iso, agora))
    conn.commit()
    conn.close()
