# -*- coding: utf-8 -*-
"""
painel_agentes/wms_caixas.py

Caixas com serial (C0000001) e codigo interno de produto (FLP000001).
Modulo de dados puro (sem Flask), mesmo estilo do wms_pedidos.py.

- criar_caixas: uma unica transacao (BEGIN IMMEDIATE ... COMMIT): contador,
  N caixas IMPRESSA, N eventos None->IMPRESSA e 1 trabalho na fila de impressao.
- reservar_numeros: sem commit, roda na transacao de quem chama.
- garantir_tabelas: DDL idempotente, sem commit (para quem abriu a conexao
  por wms.conectar / wms_pedidos.conectar).
"""
import sqlite3
from pathlib import Path

import wms
import wms_fila_impressao
import wms_pedidos
from wms import ErroWMS

SITUACOES_CAIXA = ("IMPRESSA", "EM_ESTOQUE", "ABERTA", "EXPEDIDA", "CANCELADA")
MAX_CAIXAS = 200

_DDL = (
    """
CREATE TABLE IF NOT EXISTS wms_caixas (
    serial        TEXT PRIMARY KEY,
    produto_id    INTEGER NOT NULL,
    lote          TEXT NOT NULL DEFAULT '',
    validade      TEXT,
    qtd_un        REAL NOT NULL,
    posicao       TEXT,
    situacao      TEXT NOT NULL DEFAULT 'IMPRESSA',
    serial_origem TEXT,
    impressao_id  INTEGER,
    criado_em     TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
)
""",
    "CREATE INDEX IF NOT EXISTS idx_wms_caixas_produto ON wms_caixas(produto_id, situacao)",
    """
CREATE TABLE IF NOT EXISTS wms_caixa_eventos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid           TEXT NOT NULL UNIQUE,
    serial         TEXT NOT NULL,
    de             TEXT,
    para           TEXT NOT NULL,
    posicao        TEXT,
    movimento_uuid TEXT,
    operador_id    TEXT,
    operador_nome  TEXT,
    observacao     TEXT,
    criado_em      TEXT NOT NULL
)
""",
    "CREATE INDEX IF NOT EXISTS idx_wms_caixa_eventos_serial ON wms_caixa_eventos(serial)",
    "CREATE INDEX IF NOT EXISTS idx_wms_caixa_eventos_mov ON wms_caixa_eventos(movimento_uuid)",
    """
CREATE TABLE IF NOT EXISTS wms_contadores (
    nome  TEXT PRIMARY KEY,
    valor INTEGER NOT NULL DEFAULT 0
)
""",
)


def garantir_tabelas(conn) -> None:
    """Cria tabelas de caixas, contadores e fila (idempotente). Sem commit."""
    for stmt in _DDL:
        conn.execute(stmt)
    wms_fila_impressao.garantir_tabela(conn)


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    conn = wms_pedidos.conectar(caminho)
    garantir_tabelas(conn)
    conn.commit()
    return conn


def formatar_serial(n: int) -> str:
    return f"C{int(n):07d}"


def reservar_numeros(conn, contador: str, quantidade: int) -> int:
    """Reserva `quantidade` numeros e devolve o primeiro. Sem commit."""
    quantidade = int(quantidade)
    if quantidade < 1:
        raise ErroWMS("Quantidade invalida para o contador.")
    conn.execute("INSERT OR IGNORE INTO wms_contadores (nome, valor) VALUES (?, 0)", (contador,))
    atual = conn.execute("SELECT valor FROM wms_contadores WHERE nome = ?", (contador,)).fetchone()[0]
    conn.execute("UPDATE wms_contadores SET valor = ? WHERE nome = ?", (atual + quantidade, contador))
    return atual + 1


def garantir_codigo_interno(conn, produto_id: int) -> str:
    """Devolve o codigo interno do produto (FLP000001), criando se faltar. Commit."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT codigo_interno FROM wms_produtos WHERE id = ?", (produto_id,)).fetchone()
        if not row:
            raise ErroWMS(f"Produto {produto_id} nao existe.")
        if row[0]:
            conn.commit()
            return row[0]
        codigo = f"FLP{reservar_numeros(conn, 'produto_interno', 1):06d}"
        conn.execute("UPDATE wms_produtos SET codigo_interno = ? WHERE id = ?", (codigo, produto_id))
        conn.commit()
        return codigo
    except Exception:
        conn.rollback()
        raise


def criar_caixas(conn, *, produto_id: int, lote: str, validade: str | None,
                 qtd_un: float | None, quantidade: int, operador: dict | None = None) -> dict:
    """Cria N caixas IMPRESSA e o trabalho de impressao, tudo numa transacao."""
    try:
        quantidade = int(quantidade)
    except (TypeError, ValueError):
        raise ErroWMS("Quantidade de caixas invalida.")
    if not 1 <= quantidade <= MAX_CAIXAS:
        raise ErroWMS(f"Quantidade de caixas deve ficar entre 1 e {MAX_CAIXAS}.")
    if conn.in_transaction:
        conn.commit()
    operador = operador or {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        produto = wms.obter_produto(conn, produto_id)
        if not produto:
            raise ErroWMS(f"Produto {produto_id} nao existe.")
        val = wms._validar_validade(validade)
        if produto.get("controla_validade") and not val:
            raise ErroWMS("Validade obrigatoria para este produto.")
        lote_n = " ".join(str(lote or "").split()).upper()
        if qtd_un is None:
            qtd_un = produto.get("qtd_por_caixa") or 1
        qtd_un = float(qtd_un)
        if qtd_un <= 0:
            raise ErroWMS("Quantidade por caixa deve ser maior que zero.")
        primeiro = reservar_numeros(conn, "caixa", quantidade)
        seriais = [formatar_serial(primeiro + i) for i in range(quantidade)]
        impressao_id = wms_fila_impressao.enfileirar(
            conn, tipo="CAIXA", tamanho="10x5", itens=seriais,
            titulo=f"{produto['descricao']} · {quantidade} caixas",
            pedido_por=str(operador.get("nome") or ""))
        ts = wms.agora()
        for s in seriais:
            conn.execute(
                "INSERT INTO wms_caixas (serial, produto_id, lote, validade, qtd_un, situacao, "
                "impressao_id, criado_em, atualizado_em) VALUES (?,?,?,?,?, 'IMPRESSA', ?,?,?)",
                (s, produto_id, lote_n, val, qtd_un, impressao_id, ts, ts))
            conn.execute(
                "INSERT INTO wms_caixa_eventos (uuid, serial, de, para, operador_id, operador_nome, criado_em) "
                "VALUES (?, ?, NULL, 'IMPRESSA', ?, ?, ?)",
                (f"cx-{s}-criar", s, str(operador.get("id") or ""), str(operador.get("nome") or ""), ts))
        conn.commit()
        return {"seriais": seriais, "impressao_id": impressao_id}
    except Exception:
        conn.rollback()
        raise


def obter_caixa(conn, serial: str) -> dict | None:
    cur = conn.execute("SELECT * FROM wms_caixas WHERE serial = ?", (str(serial or "").strip().upper(),))
    row = cur.fetchone()
    if not row:
        return None
    d = dict(zip([c[0] for c in cur.description], row))
    d["produto"] = wms.obter_produto(conn, d["produto_id"])
    return d


def produto_usa_caixas(produto: dict, piloto_id: int) -> bool:
    return produto.get("embarcador_id") == piloto_id
