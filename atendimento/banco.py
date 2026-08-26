# -*- coding: utf-8 -*-
"""
atendimento/banco.py

Esquema e conexão da central de atendimento (WhatsApp via Evolution API,
ver atendimento/app.py e integracao_evolution.py). Banco próprio,
dados/atendimento.db -- separado do dados/dados.db compartilhado porque
esse subsistema escreve a cada mensagem trocada (volume bem maior e
orientado a webhook, não a lote/cron como o resto do projeto).

Mesmos padrões de nucleo/banco.py:
  - CREATE TABLE IF NOT EXISTS idempotente
  - migração aditiva por PRAGMA table_info (nunca DROP, nunca renomeia)
  - timestamps TEXT em datetime('now','localtime')
"""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent
DB_PATH = _RAIZ / "dados" / "atendimento.db"

# times possíveis de uma conversa (roteamento manual, sem bot de triagem ainda)
TIMES = ("DESTINATARIOS", "MOTORISTAS", "COMERCIAL_FINANCEIRO")

_DDL = """
CREATE TABLE IF NOT EXISTS usuarios (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    login           TEXT UNIQUE NOT NULL,
    nome            TEXT NOT NULL,
    senha_hash      TEXT NOT NULL,
    papel           TEXT NOT NULL DEFAULT 'atendente',      -- admin | atendente
    ativo           INTEGER NOT NULL DEFAULT 1,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ultimo_login_em TEXT
);

CREATE TABLE IF NOT EXISTS contatos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    telefone_e164   TEXT UNIQUE NOT NULL,
    nome            TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS conversas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    protocolo               TEXT UNIQUE,                    -- AT-YYYYMMDD-00042, preenchido logo após o INSERT
    contato_id              INTEGER NOT NULL REFERENCES contatos(id),
    time                    TEXT,                            -- ver TIMES acima; NULL = não classificada ainda
    status                  TEXT NOT NULL DEFAULT 'ABERTA',  -- ABERTA | RESOLVIDA
    atendente_id            INTEGER REFERENCES usuarios(id), -- NULL = ainda não assumida
    assumida_em             TEXT,
    aberta_em               TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    encerrada_em            TEXT,
    ultima_mensagem_em      TEXT,
    ultima_mensagem_preview TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversas_status ON conversas(status, atendente_id);
CREATE INDEX IF NOT EXISTS idx_conversas_contato ON conversas(contato_id);

CREATE TABLE IF NOT EXISTS mensagens (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    conversa_id             INTEGER NOT NULL REFERENCES conversas(id) ON DELETE CASCADE,
    direcao                 TEXT NOT NULL,                   -- IN | OUT
    atendente_id            INTEGER REFERENCES usuarios(id), -- quem respondeu pela UI; NULL se IN ou se veio do próprio celular vinculado
    corpo                   TEXT,
    evolution_message_id    TEXT UNIQUE,                     -- data.key.id do webhook -- dedupe de eco/retry
    criado_em               TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_mensagens_conversa ON mensagens(conversa_id, criado_em);
"""


def conectar() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    garantir_esquema(conn)
    return conn


def garantir_esquema(conn: sqlite3.Connection):
    conn.executescript(_DDL)
    conn.commit()


def buscar_ou_criar_contato(conn: sqlite3.Connection, telefone_e164: str, nome: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM contatos WHERE telefone_e164 = ?", (telefone_e164,)).fetchone()
    if row:
        if nome and not row["nome"]:
            conn.execute("UPDATE contatos SET nome = ? WHERE id = ?", (nome, row["id"]))
            conn.commit()
        return conn.execute("SELECT * FROM contatos WHERE id = ?", (row["id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO contatos (telefone_e164, nome) VALUES (?, ?)", (telefone_e164, nome),
    )
    conn.commit()
    return conn.execute("SELECT * FROM contatos WHERE id = ?", (cur.lastrowid,)).fetchone()


def conversa_aberta_do_contato(conn: sqlite3.Connection, contato_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM conversas WHERE contato_id = ? AND status = 'ABERTA' ORDER BY id DESC LIMIT 1",
        (contato_id,),
    ).fetchone()


def abrir_conversa(conn: sqlite3.Connection, contato_id: int) -> sqlite3.Row:
    """Abre uma conversa nova e já grava o protocolo (AT-AAAAMMDD-<id com
    5 dígitos>) -- precisa do id gerado pelo INSERT, por isso o UPDATE
    logo em seguida, mesmo padrão de código auto-numerado usado alhures
    no projeto."""
    cur = conn.execute("INSERT INTO conversas (contato_id) VALUES (?)", (contato_id,))
    conversa_id = cur.lastrowid
    protocolo = f"AT-{datetime_agora_str()[:10].replace('-', '')}-{conversa_id:05d}"
    conn.execute("UPDATE conversas SET protocolo = ? WHERE id = ?", (protocolo, conversa_id))
    conn.commit()
    return conn.execute("SELECT * FROM conversas WHERE id = ?", (conversa_id,)).fetchone()


def datetime_agora_str() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def assumir_conversa(conn: sqlite3.Connection, conversa_id: int, atendente_id: int) -> bool:
    """UPDATE atômico: só assume se ainda não tinha atendente. SQLite já
    serializa escritores (busy_timeout cobre a espera), então rowcount==1
    decide quem ganhou a corrida sem precisar de lock explícito."""
    cur = conn.execute(
        "UPDATE conversas SET atendente_id = ?, assumida_em = datetime('now','localtime') "
        "WHERE id = ? AND atendente_id IS NULL",
        (atendente_id, conversa_id),
    )
    conn.commit()
    return cur.rowcount == 1


def registrar_mensagem(conn: sqlite3.Connection, conversa_id: int, direcao: str, corpo: str | None,
                        atendente_id: int | None = None, evolution_message_id: str | None = None) -> int | None:
    """Insere a mensagem e atualiza o resumo da conversa (preview + hora).
    Idempotente por evolution_message_id: se o id já existe (eco/retry do
    webhook), não duplica -- retorna None nesse caso."""
    if evolution_message_id:
        ja = conn.execute(
            "SELECT id FROM mensagens WHERE evolution_message_id = ?", (evolution_message_id,),
        ).fetchone()
        if ja:
            return None
    cur = conn.execute(
        "INSERT INTO mensagens (conversa_id, direcao, atendente_id, corpo, evolution_message_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (conversa_id, direcao, atendente_id, corpo, evolution_message_id),
    )
    preview = (corpo or "")[:120]
    conn.execute(
        "UPDATE conversas SET ultima_mensagem_em = datetime('now','localtime'), "
        "ultima_mensagem_preview = ? WHERE id = ?",
        (preview, conversa_id),
    )
    conn.commit()
    return cur.lastrowid
