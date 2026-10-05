# -*- coding: utf-8 -*-
"""
agente/banco.py

Tabela agente_acoes em dados/dados.db: tudo que o agente decidiu, propos,
enviou ou executou. APPEND-ONLY: nunca apaga, nunca reescreve texto ja
enviado -- so avanca o status.

  PROPOSTA   criada, aguardando pessoa (so acoes PROPOR)
  APROVADA   pessoa aprovou na Torre; executar.py vai rodar
  RECUSADA   pessoa recusou (fica registrado por que)
  ENVIADA    aviso ao embarcador saiu (acoes AVISAR)
  DESLIGADA  aviso que teria saido, mas o agente esta desligado no config
             (agente.ativo) -- serve pra ver o que ele faria antes de ligar
  EXECUTADA  escrita em Stokki/Vuupt feita (so depois de APROVADA)
  FALHOU     envio ou execucao falhou (resultado diz por que)

Idempotencia: UNIQUE(codigo, fato_origem, template). O mesmo fato nunca
gera a mesma acao duas vezes, em nenhuma rodada.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "dados" / "dados.db"
FMT = "%Y-%m-%d %H:%M:%S"

PROPOSTA = "PROPOSTA"
APROVADA = "APROVADA"
RECUSADA = "RECUSADA"
ENVIADA = "ENVIADA"
DESLIGADA = "DESLIGADA"
EXECUTADA = "EXECUTADA"
FALHOU = "FALHOU"
STATUS_ABERTOS = (PROPOSTA, APROVADA)


def conectar(db_path: Path | None = None) -> sqlite3.Connection:
    caminho = db_path or DB_PATH
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(caminho, timeout=30)
    conn.row_factory = sqlite3.Row
    garantir_esquema(conn)
    return conn


def garantir_esquema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agente_acoes (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo        TEXT NOT NULL,
            fato_origem   TEXT NOT NULL,
            tipo          TEXT NOT NULL,          -- AVISAR | PROPOR
            template      TEXT NOT NULL,
            destinatario  TEXT,                   -- telefone/e-mail/"torre"
            texto         TEXT,                   -- o que foi (ou seria) enviado
            dados_json    TEXT,
            status        TEXT NOT NULL,
            resultado     TEXT,
            criado_em     TEXT NOT NULL,
            atualizado_em TEXT NOT NULL,
            aprovado_por  TEXT,
            executado_em  TEXT,
            UNIQUE (codigo, fato_origem, template)
        );
        CREATE INDEX IF NOT EXISTS idx_agente_acoes_status ON agente_acoes(status);
        CREATE INDEX IF NOT EXISTS idx_agente_acoes_codigo ON agente_acoes(codigo);
        CREATE TABLE IF NOT EXISTS agente_rodadas (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            iniciada_em  TEXT NOT NULL,
            concluida_em TEXT,
            fatos        INTEGER NOT NULL DEFAULT 0,
            acoes_novas  INTEGER NOT NULL DEFAULT 0,
            enviadas     INTEGER NOT NULL DEFAULT 0,
            falhas       INTEGER NOT NULL DEFAULT 0,
            modo_teste   INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()


def _agora(agora: datetime | None) -> str:
    return (agora or datetime.now()).strftime(FMT)


def ja_existe(conn: sqlite3.Connection, codigo: str, fato_origem: str, template: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM agente_acoes WHERE codigo = ? AND fato_origem = ? AND template = ?",
        (codigo, fato_origem, template)).fetchone()
    return row is not None


def registrar(conn: sqlite3.Connection, acao, status: str, *, destinatario: str | None = None,
              texto: str | None = None, resultado: str | None = None,
              agora: datetime | None = None) -> int | None:
    """Grava a acao se ainda nao existe. Devolve o id novo, ou None quando
    ja existia (idempotencia)."""
    if ja_existe(conn, acao.codigo, acao.fato_origem, acao.template):
        return None
    ts = _agora(agora)
    cur = conn.execute("""
        INSERT INTO agente_acoes (codigo, fato_origem, tipo, template, destinatario, texto,
                                  dados_json, status, resultado, criado_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (acao.codigo, acao.fato_origem, acao.tipo, acao.template, destinatario, texto,
          json.dumps(acao.dados, ensure_ascii=False, default=str), status, resultado, ts, ts))
    conn.commit()
    return cur.lastrowid


def atualizar_status(conn: sqlite3.Connection, acao_id: int, status: str, *,
                     resultado: str | None = None, aprovado_por: str | None = None,
                     executado: bool = False, agora: datetime | None = None) -> None:
    ts = _agora(agora)
    conn.execute("""
        UPDATE agente_acoes
           SET status = ?, resultado = COALESCE(?, resultado), atualizado_em = ?,
               aprovado_por = COALESCE(?, aprovado_por),
               executado_em = CASE WHEN ? THEN ? ELSE executado_em END
         WHERE id = ?
    """, (status, resultado, ts, aprovado_por, 1 if executado else 0, ts, acao_id))
    conn.commit()


def listar(conn: sqlite3.Connection, status: str | tuple | None = None, codigo: str | None = None,
           limite: int = 200) -> list[dict]:
    sql, params = "SELECT * FROM agente_acoes WHERE 1=1", []
    if status:
        lista = (status,) if isinstance(status, str) else tuple(status)
        sql += f" AND status IN ({','.join('?' * len(lista))})"
        params += list(lista)
    if codigo:
        sql += " AND codigo = ?"
        params.append(codigo)
    sql += " ORDER BY criado_em DESC, id DESC LIMIT ?"
    params.append(limite)
    linhas = []
    for r in conn.execute(sql, params):
        d = dict(r)
        try:
            d["dados"] = json.loads(d.pop("dados_json") or "{}")
        except ValueError:
            d["dados"] = {}
        linhas.append(d)
    return linhas


def contar_propostas(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM agente_acoes WHERE status = ?", (PROPOSTA,)).fetchone()[0]


def abrir_rodada(conn: sqlite3.Connection, modo_teste: bool, agora: datetime | None = None) -> int:
    cur = conn.execute("INSERT INTO agente_rodadas (iniciada_em, modo_teste) VALUES (?, ?)",
                       (_agora(agora), 1 if modo_teste else 0))
    conn.commit()
    return cur.lastrowid


def fechar_rodada(conn: sqlite3.Connection, rodada_id: int, resumo: dict, agora: datetime | None = None) -> None:
    conn.execute("""
        UPDATE agente_rodadas SET concluida_em = ?, fatos = ?, acoes_novas = ?, enviadas = ?, falhas = ?
         WHERE id = ?
    """, (_agora(agora), resumo.get("fatos", 0), resumo.get("acoes_novas", 0),
          resumo.get("enviadas", 0), resumo.get("falhas", 0), rodada_id))
    conn.commit()
