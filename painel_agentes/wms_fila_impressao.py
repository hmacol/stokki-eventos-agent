# -*- coding: utf-8 -*-
"""
painel_agentes/wms_fila_impressao.py

Fila de impressao de etiquetas do galpao (caixas com serial).
Modulo de dados puro (sem Flask), no mesmo estilo do wms_pedidos.py.

Quem pede (tela do galpao) enfileira; quem imprime (estacao com a
impressora) lista as PENDENTES e marca como IMPRESSO.
- enfileirar NAO faz commit: roda dentro da transacao de quem chama.
- marcar_impresso faz commit e e idempotente (nao muda quem/quando imprimiu).
"""
import json

import wms

TIPOS = ("CAIXA",)
TAMANHOS = ("10x5", "10x15")
SITUACOES = ("PENDENTE", "IMPRESSO", "CANCELADO")

_DDL = (
    """
CREATE TABLE IF NOT EXISTS wms_fila_impressao (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tipo         TEXT NOT NULL,
    tamanho      TEXT NOT NULL,
    titulo       TEXT NOT NULL DEFAULT '',
    itens_json   TEXT NOT NULL,
    situacao     TEXT NOT NULL DEFAULT 'PENDENTE',
    pedido_por   TEXT NOT NULL DEFAULT '',
    criado_em    TEXT NOT NULL,
    impresso_em  TEXT,
    impresso_por TEXT
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_wms_fila_impressao_situacao
    ON wms_fila_impressao (situacao, id)
""",
)


def garantir_tabela(conn) -> None:
    """Cria a tabela se nao existir (idempotente)."""
    for stmt in _DDL:
        conn.execute(stmt)


def _consultar(conn, where: str, params: tuple, tail: str = "") -> list:
    # Le por nome via cursor.description: nao depende de row_factory.
    cur = conn.execute("SELECT * FROM wms_fila_impressao " + where + " " + tail, params)
    cols = [c[0] for c in cur.description]
    saida = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        d["itens"] = json.loads(d.pop("itens_json"))
        saida.append(d)
    return saida


def enfileirar(conn, *, tipo: str, tamanho: str, itens: list, titulo: str,
               pedido_por: str = "") -> int:
    """Insere um pedido de impressao PENDENTE. Sem commit."""
    if tipo not in TIPOS:
        raise wms.ErroWMS(f"Tipo de etiqueta invalido: {tipo!r}.")
    if tamanho not in TAMANHOS:
        raise wms.ErroWMS(f"Tamanho de etiqueta invalido: {tamanho!r}.")
    itens = [str(i) for i in (itens or [])]
    if not itens:
        raise wms.ErroWMS("Nada para imprimir: lista de itens vazia.")
    cur = conn.execute(
        "INSERT INTO wms_fila_impressao "
        "(tipo, tamanho, titulo, itens_json, situacao, pedido_por, criado_em) "
        "VALUES (?, ?, ?, ?, 'PENDENTE', ?, ?)",
        (tipo, tamanho, titulo or "", json.dumps(itens, ensure_ascii=False),
         pedido_por or "", wms.agora()))
    return cur.lastrowid


def obter(conn, impressao_id: int):
    rows = _consultar(conn, "WHERE id = ?", (impressao_id,))
    return rows[0] if rows else None


def listar(conn, situacao: str = "PENDENTE", limite: int = 50) -> list:
    """Das mais antigas para as mais novas."""
    return _consultar(conn, "WHERE situacao = ?", (situacao,),
                      "ORDER BY id ASC LIMIT %d" % int(limite))


def marcar_impresso(conn, impressao_id: int, por: str) -> dict:
    """PENDENTE -> IMPRESSO, com commit. Repetir nao muda impresso_em/por."""
    atual = obter(conn, impressao_id)
    if atual is None:
        raise wms.ErroWMS(f"Pedido de impressao {impressao_id} nao existe.")
    if atual["situacao"] == "CANCELADO":
        raise wms.ErroWMS(f"Pedido de impressao {impressao_id} foi cancelado.")
    if atual["situacao"] == "PENDENTE":
        conn.execute(
            "UPDATE wms_fila_impressao SET situacao = 'IMPRESSO', "
            "impresso_em = ?, impresso_por = ? WHERE id = ? AND situacao = 'PENDENTE'",
            (wms.agora(), por or "", impressao_id))
        conn.commit()
    return obter(conn, impressao_id)
