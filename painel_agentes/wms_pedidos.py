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


def disponivel_por_lote(conn, produto_id: int) -> list[dict]:
    """
    Saldo de um produto por (posicao, lote, validade), ja descontando as
    reservas ATIVAS. Ordem FEFO: quem vence primeiro vem primeiro; sem
    validade vai pro fim.

    O disponivel e sempre derivado -- wms_saldos nunca e tocado pela reserva.
    """
    rows = conn.execute("""
        SELECT s.posicao, s.lote, s.validade, s.quantidade AS saldo,
               COALESCE((SELECT SUM(r.quantidade_un) FROM wms_reservas r
                          WHERE r.produto_id = s.produto_id AND r.posicao = s.posicao
                            AND r.lote = s.lote AND r.validade = s.validade
                            AND r.estado = 'ATIVA'), 0) AS reservado
          FROM wms_saldos s
          JOIN wms_posicoes p ON p.codigo = s.posicao AND p.ativo = 1
         WHERE s.produto_id = ? AND s.quantidade > 0
         ORDER BY s.validade = '', s.validade, s.posicao""", (int(produto_id),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["saldo"] = round(float(d["saldo"]), 3)
        d["reservado"] = round(float(d["reservado"]), 3)
        d["disponivel"] = round(d["saldo"] - d["reservado"], 3)
        out.append(d)
    return out


def alocar_fefo(conn, produto_id: int, qtd_un: float) -> tuple[list[dict], float]:
    """
    Distribui qtd_un pelos lotes disponiveis, do que vence primeiro pro que
    vence depois. Devolve (alocacoes, faltante).

    Faltar saldo nao e erro (decisao do Hugo, 21/09): aloca o que da e diz
    quanto faltou, pra virar pendencia.
    """
    restante = round(float(qtd_un), 3)
    alocacoes = []
    for linha in disponivel_por_lote(conn, produto_id):
        if restante <= 0:
            break
        se_pega = min(linha["disponivel"], restante)
        if se_pega <= 0:
            continue
        alocacoes.append({"posicao": linha["posicao"], "lote": linha["lote"],
                          "validade": linha["validade"], "quantidade_un": round(se_pega, 3)})
        restante = round(restante - se_pega, 3)
    return alocacoes, max(restante, 0.0)


def registrar_pedido(conn, pedido: dict, itens: list[dict]) -> int:
    """
    Cria ou atualiza o espelho do pedido e as linhas de item, ja resolvendo
    produto e unidade. Idempotente por id_stokki + linha.

    Linha que existia numa chamada anterior e nao vem em `itens` desta vez
    (o pedido foi editado na Stokki e a linha saiu) tem suas reservas ATIVAS
    canceladas -- a linha em si NAO e apagada de wms_pedido_itens, o
    historico importa; so a reserva e liberada, pra nao esconder estoque
    de um item que nao existe mais no pedido.
    """
    id_stokki = int(pedido["id_stokki"])
    agora = wms.agora()
    row = conn.execute("SELECT id FROM wms_pedidos WHERE id_stokki = ?", (id_stokki,)).fetchone()
    if row:
        pedido_id = row["id"]
        conn.execute("UPDATE wms_pedidos SET situacao = ?, embarcador = ?, atualizado_em = ? WHERE id = ?",
                     (pedido.get("situacao", ""), pedido.get("embarcador", ""), agora, pedido_id))
    else:
        cur = conn.execute("""
            INSERT INTO wms_pedidos (id_stokki, codigo_ps, embarcador, situacao, estado_reserva, lido_em, atualizado_em)
            VALUES (?,?,?,?,'PENDENTE',?,?)""",
            (id_stokki, pedido.get("codigo_ps", ""), pedido.get("embarcador", ""),
             pedido.get("situacao", ""), agora, agora))
        pedido_id = cur.lastrowid

    linhas_novas = [int(item["linha"]) for item in itens]

    for item in itens:
        r = resolver_item(conn, item)
        conn.execute("""
            INSERT INTO wms_pedido_itens (pedido_id, linha, sku, ean_linha, descricao, qtd_embalagem,
                                          qtd_un, produto_id, motivo_pendencia)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pedido_id, linha) DO UPDATE SET
                sku = excluded.sku, ean_linha = excluded.ean_linha, descricao = excluded.descricao,
                qtd_embalagem = excluded.qtd_embalagem, qtd_un = excluded.qtd_un,
                produto_id = excluded.produto_id, motivo_pendencia = excluded.motivo_pendencia""",
            (pedido_id, int(item["linha"]), item.get("sku", ""), item.get("ean_linha", ""),
             item.get("descricao", ""), float(item["qtd_embalagem"]),
             r["qtd_un"], r["produto_id"], r["motivo_pendencia"]))

    if linhas_novas:
        placeholders = ",".join("?" * len(linhas_novas))
        sumidas = conn.execute(
            f"SELECT id FROM wms_pedido_itens WHERE pedido_id = ? AND linha NOT IN ({placeholders})",
            (pedido_id, *linhas_novas)).fetchall()
    else:
        sumidas = conn.execute(
            "SELECT id FROM wms_pedido_itens WHERE pedido_id = ?", (pedido_id,)).fetchall()
    for it in sumidas:
        conn.execute(
            "UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? "
            "WHERE item_id = ? AND estado = 'ATIVA'", (agora, it["id"]))
    return pedido_id


def reservar_pedido(conn, pedido_id: int) -> dict:
    """
    Reserva FEFO o que cada item do pedido precisa. Nao mexe em saldo fisico.

    Reconciliacao (nao so idempotencia -- correcao 21/09, rodada 1): compara
    o total ATIVO reservado do item com a qtd_un atual, os dois arredondados
    a 3 casas.
      - iguais -> nada a fazer (caso normal de rodada repetida a cada 15 min).
      - diferentes e todas as reservas ATIVAS do item sao origem FEFO ->
        cancela e realoca do zero pra qtd_un atual. Cobre o pedido editado
        na Stokki entre rodadas, pra quantidade maior (reserva ficava presa
        no valor velho) ou menor (reserva sobrando escondia estoque que
        existe de verdade no galpao).
      - diferentes mas existe reserva ATIVA origem MANUAL -> nao mexe em
        nada, so registra pendencia. MANUAL quer dizer que alguem ja bipou
        aquele lote no galpao; o sistema nao desfaz isso por conta propria.

    Item que nao resolve produto ou unidade nao vira reserva -- vira
    pendencia. Falta de saldo nunca levanta erro -- vira pendencia e o
    pedido fica PARCIAL (decisao do Hugo, 21/09: avisar, nunca bloquear).

    A comparacao que decide RESERVADO x PARCIAL arredonda a soma das
    reservas a 3 casas (ROUND(...,3) no SQL): cada alocacao do FEFO ja vem
    arredondada individualmente, mas a SOMA de varias delas pode acumular
    residuo de ponto flutuante (ex.: 0.1 + 0.2 + 0.3 = 0.6000000000000001)
    e marcar como PARCIAL um pedido que na pratica esta 100% reservado.
    """
    agora = wms.agora()
    pendencias = []
    criadas = 0
    itens = conn.execute("SELECT * FROM wms_pedido_itens WHERE pedido_id = ? ORDER BY linha",
                         (pedido_id,)).fetchall()
    for item in itens:
        reservas_ativas = conn.execute(
            "SELECT * FROM wms_reservas WHERE item_id = ? AND estado = 'ATIVA'",
            (item["id"],)).fetchall()

        if reservas_ativas and item["qtd_un"] is not None:
            total_ativo = round(sum(r["quantidade_un"] for r in reservas_ativas), 3)
            qtd_atual = round(item["qtd_un"], 3)
            if total_ativo != qtd_atual:
                if any(r["origem"] != "FEFO" for r in reservas_ativas):
                    pendencias.append(
                        f"linha {item['linha']} ({item['descricao']}): quantidade mudou de "
                        f"{total_ativo:g} para {qtd_atual:g} UN depois de uma reserva MANUAL -- "
                        f"ajuste o lote manualmente")
                    continue
                for r in reservas_ativas:
                    conn.execute(
                        "UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE id = ?",
                        (agora, r["id"]))
                reservas_ativas = []

        if reservas_ativas:
            continue
        if not item["produto_id"] or item["qtd_un"] is None:
            pendencias.append(f"linha {item['linha']}: {item['motivo_pendencia']}")
            continue
        alocacoes, faltou = alocar_fefo(conn, item["produto_id"], item["qtd_un"])
        for a in alocacoes:
            conn.execute("""
                INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade,
                                          quantidade_un, estado, origem, criado_em, atualizado_em)
                VALUES (?,?,?,?,?,?,?,'ATIVA','FEFO',?,?)""",
                (pedido_id, item["id"], item["produto_id"], a["posicao"], a["lote"], a["validade"],
                 a["quantidade_un"], agora, agora))
            criadas += 1
        if faltou > 0:
            pendencias.append(
                f"linha {item['linha']} ({item['descricao']}): faltaram {faltou:g} UN em estoque")

    total_itens = len(itens)
    resolvidos = conn.execute("""
        SELECT COUNT(*) n FROM wms_pedido_itens i
         WHERE i.pedido_id = ? AND i.qtd_un IS NOT NULL
           AND ROUND(i.qtd_un, 3) <= ROUND(COALESCE((SELECT SUM(r.quantidade_un) FROM wms_reservas r
                                      WHERE r.item_id = i.id AND r.estado IN ('ATIVA','CONSUMIDA')), 0), 3)
        """, (pedido_id,)).fetchone()["n"]
    estado = "RESERVADO" if total_itens and resolvidos == total_itens else "PARCIAL"
    conn.execute("UPDATE wms_pedidos SET estado_reserva = ?, atualizado_em = ? WHERE id = ?",
                 (estado, agora, pedido_id))
    return {"estado": estado, "reservas": criadas, "pendencias": pendencias}


def cancelar_reservas(conn, pedido_id: int, motivo: str = "") -> int:
    """Libera as reservas ATIVAS do pedido e marca o pedido como CANCELADO."""
    agora = wms.agora()
    cur = conn.execute(
        "UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE pedido_id = ? AND estado = 'ATIVA'",
        (agora, pedido_id))
    conn.execute("UPDATE wms_pedidos SET estado_reserva = 'CANCELADO', atualizado_em = ? WHERE id = ?",
                 (agora, pedido_id))
    return cur.rowcount
