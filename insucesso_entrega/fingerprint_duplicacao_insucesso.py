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
    # cancelado_em (11/08): quando o remetente responde pedindo pra NÃO
    # reenviar, a reentrega é cancelada no VUUPT e registrada aqui. A
    # linha NUNCA é removida -- é ela que garante que o insucesso não
    # será duplicado de novo nas execuções seguintes.
    try:
        conn.execute("ALTER TABLE insucessos_duplicados ADD COLUMN cancelado_em TEXT")
    except sqlite3.OperationalError:
        pass  # coluna já existe
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


def buscar_novo_code(service_id_original: int) -> str | None:
    """Código do serviço DUPLICADO (a reentrega) criado pra este
    insucesso, ou None se nunca foi duplicado. Usado pra localizar a
    reentrega no VUUPT quando o remetente pede cancelamento."""
    conn = _conectar()
    row = conn.execute(
        "SELECT novo_code FROM insucessos_duplicados WHERE service_id_original = ?",
        (service_id_original,),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def marcar_cancelado(service_id_original: int):
    """Registra que a reentrega deste insucesso foi CANCELADA no VUUPT
    (a pedido do remetente). A linha permanece na tabela -- o
    ja_duplicado() continua True, então o insucesso nunca é duplicado
    de novo (pedido do Hugo, 11/08: 'incluir no fingerprint para não
    ser importado novamente')."""
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute(
        "UPDATE insucessos_duplicados SET cancelado_em = ? WHERE service_id_original = ?",
        (agora, service_id_original),
    )
    conn.commit()
    conn.close()
