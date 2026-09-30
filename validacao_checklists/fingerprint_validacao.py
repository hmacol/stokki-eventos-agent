# -*- coding: utf-8 -*-
"""
fingerprint_validacao.py

Registro local de quais checklists (canhotos) já foram ANALISADOS pelo
agente de validação -- evita reanalisar (e pagar API de novo) o mesmo
checklist a cada execução, e evita clonar duas vezes o mesmo pedido
reprovado. Segue o padrão do fingerprint_duplicacao_insucesso.py.

A linha NUNCA é removida: um checklist reprovado ou em dúvida continua
com validated_at=null no VUUPT (validação manual pendente), então ele
reapareceria na janela de busca de todas as execuções seguintes.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent  # subpasta validacao_checklists/ -- sobe pro dados/dados.db compartilhado
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS checklists_analisados (
            checklist_id  INTEGER PRIMARY KEY,
            service_id    INTEGER NOT NULL,
            code          TEXT NOT NULL,
            decisao       TEXT NOT NULL,
            motivo        TEXT,
            novo_code     TEXT,
            analisado_em  TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def ja_analisado(checklist_id: int) -> bool:
    """True se este checklist já passou pela análise do agente."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM checklists_analisados WHERE checklist_id = ?",
        (checklist_id,),
    ).fetchone()
    conn.close()
    return row is not None


def registrar(checklist_id: int, service_id: int, code: str,
              decisao: str, motivo: str = "", novo_code: str | None = None):
    """Grava a decisão final do agente sobre o checklist.

    decisao: 'aprovada' | 'reprovada' | 'duvida'
    novo_code: código do serviço clonado no VUUPT (só quando reprovada).
    """
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO checklists_analisados
            (checklist_id, service_id, code, decisao, motivo, novo_code, analisado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(checklist_id) DO NOTHING
    """, (checklist_id, service_id, code, decisao, motivo, novo_code, agora))
    conn.commit()
    conn.close()
