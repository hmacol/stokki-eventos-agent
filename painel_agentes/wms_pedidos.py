# -*- coding: utf-8 -*-
"""
painel_agentes/wms_pedidos.py

Estoque com reserva por pedido (WMS fase 2, Hugo 21/09/2026).
Modulo de dados puro (sem Flask), no mesmo estilo do wms.py.

Regras que este modulo garante:
- Reserva NAO altera saldo fisico. wms_saldos continua sendo exatamente a
  soma de wms_movimentos; o disponivel e derivado
  (saldo - reservas ATIVAS).
- Item que nao resolve produto ou unidade nao vira reserva nem baixa:
  vira pendencia. Estoque que mente e pior que estoque nenhum.
- Falta de saldo nunca bloqueia a operacao: reserva o que da e registra a
  falta (decisao do Hugo, 21/09).
- A baixa reusa wms.registrar_movimento(tipo="SAIDA") com uuid
  deterministico, entao rodar duas vezes nao baixa duas vezes.
"""
import sqlite3
from pathlib import Path

import wms

ESTADOS_PEDIDO = ("PENDENTE", "RESERVADO", "PARCIAL", "BAIXADO", "CANCELADO")
ESTADOS_RESERVA = ("ATIVA", "CONSUMIDA", "CANCELADA")

_DDL = """
CREATE TABLE IF NOT EXISTS wms_pedidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    id_stokki       INTEGER NOT NULL UNIQUE,
    codigo_ps       TEXT NOT NULL,
    embarcador      TEXT NOT NULL DEFAULT '',
    situacao        TEXT NOT NULL DEFAULT '',
    estado_reserva  TEXT NOT NULL DEFAULT 'PENDENTE',
    lido_em         TEXT NOT NULL,
    atualizado_em   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_pedidos_estado ON wms_pedidos(estado_reserva);
CREATE INDEX IF NOT EXISTS idx_wms_pedidos_ps ON wms_pedidos(codigo_ps);
CREATE TABLE IF NOT EXISTS wms_pedido_itens (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id        INTEGER NOT NULL REFERENCES wms_pedidos(id),
    linha            INTEGER NOT NULL,
    sku              TEXT NOT NULL DEFAULT '',
    ean_linha        TEXT NOT NULL DEFAULT '',
    descricao        TEXT NOT NULL DEFAULT '',
    qtd_embalagem    REAL NOT NULL,
    qtd_un           REAL,
    produto_id       INTEGER REFERENCES wms_produtos(id),
    motivo_pendencia TEXT NOT NULL DEFAULT '',
    UNIQUE (pedido_id, linha)
);
CREATE TABLE IF NOT EXISTS wms_reservas (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id      INTEGER NOT NULL REFERENCES wms_pedidos(id),
    item_id        INTEGER NOT NULL REFERENCES wms_pedido_itens(id),
    produto_id     INTEGER NOT NULL REFERENCES wms_produtos(id),
    posicao        TEXT NOT NULL,
    lote           TEXT NOT NULL DEFAULT '',
    validade       TEXT NOT NULL DEFAULT '',
    quantidade_un  REAL NOT NULL,
    estado         TEXT NOT NULL DEFAULT 'ATIVA',
    origem         TEXT NOT NULL DEFAULT 'FEFO',
    movimento_uuid TEXT NOT NULL DEFAULT '',
    criado_em      TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_reservas_saldo
    ON wms_reservas(produto_id, posicao, lote, validade, estado);
CREATE INDEX IF NOT EXISTS idx_wms_reservas_pedido ON wms_reservas(pedido_id, estado);
"""


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    """Conexao com as tabelas da fase 1 (wms.conectar) mais as da fase 2."""
    conn = wms.conectar(caminho)
    conn.executescript(_DDL)
    return conn


def _so_digitos(valor) -> str:
    return "".join(c for c in str(valor or "") if c.isdigit())


def resolver_item(conn, item: dict) -> dict:
    """
    Descobre a que produto do catalogo a linha do pedido se refere e converte
    a quantidade de EMBALAGEM pra UN.

    A quantidade da Stokki e por embalagem e o EAN da linha diz QUAL
    embalagem. Ordem das regras (spec secao 6):
      1. EAN da linha == DUN do produto  -> qtd * qtd_por_caixa
      2. EAN da linha == EAN unitario    -> qtd
      3. SKU identifica um unico produto ativo com qtd_por_caixa == 1 -> qtd
      4. qualquer outro caso -> pendencia, sem quantidade

    Nunca chuta: um item que cai na regra 4 nao vira reserva nem baixa.
    """
    ean = _so_digitos(item.get("ean_linha"))
    sku = str(item.get("sku") or "").strip()
    qtd = float(item.get("qtd_embalagem") or 0)

    if ean:
        for coluna, fator_caixa in (("dun", True), ("ean", False)):
            rows = conn.execute(
                f"SELECT * FROM wms_produtos WHERE ativo = 1 AND {coluna} = ?", (ean,)).fetchall()
            if len(rows) == 1:
                p = rows[0]
                por_caixa = float(p["qtd_por_caixa"] or 1)
                return {"produto_id": p["id"],
                        "qtd_un": round(qtd * por_caixa, 3) if fator_caixa else qtd,
                        "motivo_pendencia": ""}
            if len(rows) > 1:
                return {"produto_id": None, "qtd_un": None,
                        "motivo_pendencia": f"EAN {ean} esta em {len(rows)} produtos ativos"}

    if sku:
        rows = conn.execute(
            "SELECT * FROM wms_produtos WHERE ativo = 1 AND sku = ? COLLATE NOCASE", (sku,)).fetchall()
        if len(rows) == 1:
            p = rows[0]
            por_caixa = float(p["qtd_por_caixa"] or 1)
            if por_caixa == 1:
                return {"produto_id": p["id"], "qtd_un": qtd, "motivo_pendencia": ""}
            return {"produto_id": p["id"], "qtd_un": None,
                    "motivo_pendencia": (f"EAN {ean or '(vazio)'} nao e o unitario nem o DUN do SKU {sku}; "
                                         f"sem saber a unidade, nao da pra converter")}
        if len(rows) > 1:
            return {"produto_id": None, "qtd_un": None,
                    "motivo_pendencia": f"SKU {sku} esta em {len(rows)} produtos ativos"}

    return {"produto_id": None, "qtd_un": None,
            "motivo_pendencia": f"Produto nao encontrado no catalogo (SKU {sku or '-'}, EAN {ean or '-'})"}
