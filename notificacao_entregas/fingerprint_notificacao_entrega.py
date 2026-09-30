# -*- coding: utf-8 -*-
"""
notificacao_entregas/fingerprint_notificacao_entrega.py

Estado da notificação de entrega concluída, 1 linha por serviço da Vuupt
(service_id) -- reentrega ('-R2') é outro serviço, então ganha a própria
linha e o próprio e-mail. É o que garante 1 e-mail por pedido mesmo com
o timer relendo a mesma janela a cada 5 min. Estados em regras_entrega.py.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from notificacao_entregas.regras_entrega import (
    ESTADO_ENVIADO, ESTADO_ERRO_ENVIO, ESTADO_FALHA_ENVIO, MAX_TENTATIVAS_ENVIO, codigo_limpo,
)

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "dados.db"


def conectar(db_path: Path | None = None) -> sqlite3.Connection:
    caminho = Path(db_path or DB_PATH)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notificacoes_entrega (
            service_id        INTEGER PRIMARY KEY,
            codigo            TEXT,
            sender_id         INTEGER,
            status_done       TEXT,
            completed_at      TEXT,
            estado            TEXT NOT NULL,
            motivo_estado     TEXT,
            com_canhoto       INTEGER NOT NULL DEFAULT 0,
            destinatarios     TEXT,
            tentativas_envio  INTEGER NOT NULL DEFAULT 0,
            primeira_vista_em TEXT NOT NULL,
            atualizado_em     TEXT NOT NULL,
            enviado_em        TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notificacoes_entrega_estado ON notificacoes_entrega(estado)")
    conn.commit()
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def estados(conn: sqlite3.Connection, service_ids: list[int]) -> dict[int, str]:
    resultado: dict[int, str] = {}
    ids = [int(i) for i in service_ids if i]
    for i in range(0, len(ids), 900):
        lote = ids[i:i + 900]
        marc = ",".join("?" * len(lote))
        for row in conn.execute(
                f"SELECT service_id, estado FROM notificacoes_entrega WHERE service_id IN ({marc})", lote):
            resultado[row["service_id"]] = row["estado"]
    return resultado


def registrar(conn: sqlite3.Connection, servico: dict, estado: str, motivo: str = "",
              com_canhoto: bool = False, destinatarios: str = "") -> None:
    agora = _agora()
    conn.execute("""
        INSERT INTO notificacoes_entrega
            (service_id, codigo, sender_id, status_done, completed_at, estado, motivo_estado,
             com_canhoto, destinatarios, primeira_vista_em, atualizado_em, enviado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(service_id) DO UPDATE SET
            estado = excluded.estado, motivo_estado = excluded.motivo_estado,
            status_done = excluded.status_done, completed_at = excluded.completed_at,
            com_canhoto = excluded.com_canhoto, destinatarios = excluded.destinatarios,
            atualizado_em = excluded.atualizado_em, enviado_em = excluded.enviado_em
    """, (servico.get("id"), codigo_limpo(servico.get("code")), servico.get("sender_id"),
          servico.get("status_done"), servico.get("completed_at"), estado, motivo or None,
          1 if com_canhoto else 0, destinatarios or None, agora, agora,
          agora if estado == ESTADO_ENVIADO else None))
    conn.commit()


def registrar_falha_envio(conn: sqlite3.Connection, servico: dict) -> str:
    """SMTP falhou: conta a tentativa. Devolve o estado novo -- FALHA_ENVIO
    (tenta de novo na próxima rodada) ou ERRO_ENVIO (desistiu)."""
    row = conn.execute("SELECT tentativas_envio FROM notificacoes_entrega WHERE service_id = ?",
                       (servico.get("id"),)).fetchone()
    tentativas = (row["tentativas_envio"] if row else 0) + 1
    estado = ESTADO_ERRO_ENVIO if tentativas >= MAX_TENTATIVAS_ENVIO else ESTADO_FALHA_ENVIO
    registrar(conn, servico, estado, motivo=f"falha de SMTP ({tentativas}x)")
    conn.execute("UPDATE notificacoes_entrega SET tentativas_envio = ? WHERE service_id = ?",
                 (tentativas, servico.get("id")))
    conn.commit()
    return estado
