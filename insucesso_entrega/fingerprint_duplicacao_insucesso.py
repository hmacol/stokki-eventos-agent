# -*- coding: utf-8 -*-
"""
fingerprint_duplicacao_insucesso.py

Registro local de quais serviços com insucesso já foram duplicados --
evita duplicar o MESMO insucesso de novo se ele ainda aparecer na
janela de horas de buscar_servicos_insucesso() numa execução seguinte
(a duplicação em si é uma ação real no VUUPT, não pode repetir).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # agora numa subpasta (insucesso_entrega/) -- sobe pra achar dados/dados.db compartilhado
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS insucessos_duplicados (
            service_id_original   INTEGER PRIMARY KEY,
            novo_code             TEXT NOT NULL,
            duplicado_em          TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def ja_duplicado(service_id_original: int) -> bool:
    """True se este serviço (o ORIGINAL que falhou) já foi duplicado antes."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM insucessos_duplicados WHERE service_id_original = ?",
        (service_id_original,),
    ).fetchone()
    conn.close()
    return row is not None


def marcar_duplicado(service_id_original: int, novo_code: str):
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO insucessos_duplicados (service_id_original, novo_code, duplicado_em)
        VALUES (?, ?, ?)
        ON CONFLICT(service_id_original) DO NOTHING
    """, (service_id_original, novo_code, agora))
    conn.commit()
    conn.close()
