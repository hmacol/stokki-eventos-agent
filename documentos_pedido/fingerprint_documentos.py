# -*- coding: utf-8 -*-
"""
fingerprint_documentos.py

Registro local de documentos já processados (baixados, classificados,
casados com um pedido e enviados pro GCS) -- evita reprocessar o
mesmo arquivo em execuções seguintes, tanto vindo de e-mail quanto da
Stokki.

Chave de identidade: hash SHA-256 do conteúdo do arquivo (não do nome
-- o mesmo documento pode chegar com nomes diferentes por e-mail e
pela Stokki, mas o CONTEÚDO é idêntico).
"""
import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS documentos_processados (
            hash_conteudo   TEXT PRIMARY KEY,
            origem          TEXT NOT NULL,
            nome_arquivo    TEXT,
            tipo            TEXT,
            codigo_pedido   TEXT,
            status          TEXT NOT NULL,
            motivo          TEXT,
            gcs_path        TEXT,
            processado_em   TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def calcular_hash(caminho_arquivo: Path) -> str:
    """SHA-256 do conteúdo do arquivo -- identidade real do documento,
    independente do nome."""
    h = hashlib.sha256()
    with open(caminho_arquivo, "rb") as f:
        for bloco in iter(lambda: f.read(8192), b""):
            h.update(bloco)
    return h.hexdigest()


def ja_processado(hash_conteudo: str) -> bool:
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM documentos_processados WHERE hash_conteudo = ?", (hash_conteudo,)
    ).fetchone()
    conn.close()
    return row is not None


def marcar_processado(hash_conteudo: str, origem: str, nome_arquivo: str, tipo: str | None,
                      codigo_pedido: str | None, status: str, motivo: str | None = None,
                      gcs_path: str | None = None):
    """
    status: 'ENVIADO' (casou com pedido e foi pro GCS) ou
    'REVISAO_MANUAL' (não conseguiu casar com nenhum pedido -- motivo
    explica por quê).
    """
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO documentos_processados
            (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, status, motivo, gcs_path, processado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hash_conteudo) DO UPDATE SET
            status = excluded.status, motivo = excluded.motivo,
            gcs_path = excluded.gcs_path, processado_em = excluded.processado_em
    """, (hash_conteudo, origem, nome_arquivo, tipo, codigo_pedido, status, motivo, gcs_path, agora))
    conn.commit()
    conn.close()


def ja_enviado_para_pedido(codigo_pedido: str, tipo: str) -> bool:
    """True se já existe um documento desse tipo enviado com sucesso pra
    esse pedido -- usado pra pular pedido sem precisar reabrir a página
    no Stokki e regerar o documento (ex: DANFE) de novo à toa."""
    conn = _conectar()
    row = conn.execute(
        "SELECT 1 FROM documentos_processados WHERE codigo_pedido = ? AND tipo = ? AND status = 'ENVIADO'",
        (codigo_pedido, tipo),
    ).fetchone()
    conn.close()
    return row is not None


def listar_pendentes_revisao(limite: int = 100) -> list[dict]:
    """Documentos que não conseguiram ser casados com nenhum pedido --
    pra revisão manual do Hugo."""
    conn = _conectar()
    rows = conn.execute(
        "SELECT * FROM documentos_processados WHERE status = 'REVISAO_MANUAL' "
        "ORDER BY processado_em DESC LIMIT ?", (limite,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
