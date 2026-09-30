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
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import wms

ESTADOS_PEDIDO = ("PENDENTE", "RESERVADO", "PARCIAL", "BAIXADO", "CANCELADO")
ESTADOS_RESERVA = ("ATIVA", "CONSUMIDA", "CANCELADA")

# A partir de quantos dias uma reserva ATIVA parada vira sintoma na tela da
# equipe (ver reservas_antigas). Sobrescrevivel em config.yaml, secao wms,
# chave reserva_antiga_dias -- a rota do painel le de la.
RESERVA_ANTIGA_DIAS = 4

_DDL = """
CREATE TABLE IF NOT EXISTS wms_pedidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    id_stokki       INTEGER NOT NULL UNIQUE,
    codigo_ps       TEXT NOT NULL,
    embarcador      TEXT NOT NULL DEFAULT '',
    situacao        TEXT NOT NULL DEFAULT '',
    estado_reserva  TEXT NOT NULL DEFAULT 'PENDENTE',
    motivo_cancelamento TEXT NOT NULL DEFAULT '',  -- por que as reservas foram liberadas
    saldo_negativo  TEXT NOT NULL DEFAULT '',      -- baixa que deixou posicao negativa (aviso pra equipe)
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
    falta_un         REAL NOT NULL DEFAULT 0,   -- quanto faltou de saldo na ultima reserva
    removido_em      TEXT NOT NULL DEFAULT '',  -- linha que saiu do pedido na Stokki: nao reserva mais
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
CREATE TABLE IF NOT EXISTS wms_recebimentos (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    id_stokki      INTEGER NOT NULL UNIQUE,
    codigo         TEXT NOT NULL DEFAULT '',
    embarcador     TEXT NOT NULL DEFAULT '',
    stkkc_id       TEXT NOT NULL DEFAULT '',          -- #stkkc-<id> do embarcador na Stokki
    situacao       TEXT NOT NULL DEFAULT '',
    chegada        TEXT NOT NULL DEFAULT '',
    estado         TEXT NOT NULL DEFAULT 'ESPERADO',  -- ESPERADO | ENDERECADO | DIVERGENCIA
    observacao_divergencia TEXT NOT NULL DEFAULT '',  -- o que o operador viu ("chegou avariado")
    encerrado_em   TEXT NOT NULL DEFAULT '',
    encerrado_por  TEXT NOT NULL DEFAULT '',
    lido_em        TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wms_recebimentos_estado ON wms_recebimentos(estado);
CREATE TABLE IF NOT EXISTS wms_recebimento_itens (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recebimento_id      INTEGER NOT NULL REFERENCES wms_recebimentos(id),
    linha               INTEGER NOT NULL,
    sku                 TEXT NOT NULL DEFAULT '',
    ean_linha           TEXT NOT NULL DEFAULT '',
    descricao           TEXT NOT NULL DEFAULT '',
    qtd_embalagem       REAL NOT NULL,
    qtd_un              REAL,
    produto_id          INTEGER REFERENCES wms_produtos(id),
    motivo_pendencia    TEXT NOT NULL DEFAULT '',
    qtd_enderecada      REAL NOT NULL DEFAULT 0,
    falta_un            REAL NOT NULL DEFAULT 0,   -- congelado no encerramento com divergencia
    UNIQUE (recebimento_id, linha)
);
CREATE INDEX IF NOT EXISTS idx_wms_recebimento_itens_recebimento ON wms_recebimento_itens(recebimento_id);
CREATE TABLE IF NOT EXISTS wms_recebimento_enderecamentos (
    uuid           TEXT PRIMARY KEY,   -- o mesmo uuid do movimento de ENTRADA do aparelho
    recebimento_id INTEGER NOT NULL REFERENCES wms_recebimentos(id),
    item_id        INTEGER NOT NULL REFERENCES wms_recebimento_itens(id),
    quantidade     REAL NOT NULL,
    criado_em      TEXT NOT NULL
);
"""

# Colunas que nasceram depois da primeira versao das tabelas. CREATE TABLE
# IF NOT EXISTS nao mexe em tabela que ja existe, entao banco antigo (a VPS)
# precisa do ALTER TABLE -- feito uma vez por conexao, e barato.
_COLUNAS_NOVAS = {
    "wms_pedidos": {"motivo_cancelamento": "TEXT NOT NULL DEFAULT ''",
                    "saldo_negativo": "TEXT NOT NULL DEFAULT ''"},
    "wms_pedido_itens": {"falta_un": "REAL NOT NULL DEFAULT 0",
                         "removido_em": "TEXT NOT NULL DEFAULT ''"},
    "wms_recebimentos": {"stkkc_id": "TEXT NOT NULL DEFAULT ''",
                         "observacao_divergencia": "TEXT NOT NULL DEFAULT ''",
                         "encerrado_em": "TEXT NOT NULL DEFAULT ''",
                         "encerrado_por": "TEXT NOT NULL DEFAULT ''"},
    "wms_recebimento_itens": {"falta_un": "REAL NOT NULL DEFAULT 0"},
}


def _migrar(conn) -> None:
    for tabela, colunas in _COLUNAS_NOVAS.items():
        existentes = {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}
        for nome, tipo in colunas.items():
            if nome not in existentes:
                conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")
    conn.commit()


def conectar(caminho: Path | None = None) -> sqlite3.Connection:
    """Conexao com as tabelas da fase 1 (wms.conectar) mais as da fase 2."""
    conn = wms.conectar(caminho)
    conn.executescript(_DDL)
    _migrar(conn)
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
    reservas ATIVAS. Ordem FEFO (spec 7.3 item 4): quem vence primeiro vem
    primeiro; empatou a validade, sai primeiro quem ENTROU primeiro (a
    primeira ENTRADA daquele lote naquela posicao); sem validade vai pro
    fim. A posicao so entra como desempate final, pra ordem ser estavel.

    O disponivel e sempre derivado -- wms_saldos nunca e tocado pela reserva.
    """
    rows = conn.execute("""
        SELECT s.posicao, s.lote, s.validade, s.quantidade AS saldo,
               COALESCE((SELECT SUM(r.quantidade_un) FROM wms_reservas r
                          WHERE r.produto_id = s.produto_id AND r.posicao = s.posicao
                            AND r.lote = s.lote AND r.validade = s.validade
                            AND r.estado = 'ATIVA'), 0) AS reservado,
               COALESCE((SELECT MIN(m.criado_em) FROM wms_movimentos m
                          WHERE m.produto_id = s.produto_id AND m.posicao_destino = s.posicao
                            AND m.lote = s.lote AND COALESCE(m.validade, '') = s.validade
                            AND m.tipo = 'ENTRADA'), '9999-12-31') AS primeira_entrada
          FROM wms_saldos s
          JOIN wms_posicoes p ON p.codigo = s.posicao AND p.ativo = 1
         WHERE s.produto_id = ? AND s.quantidade > 0
         ORDER BY s.validade = '', s.validade, primeira_entrada, s.posicao""",
        (int(produto_id),)).fetchall()
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


def _gravar_falta(conn, item_id: int, faltou: float) -> None:
    """
    Persiste quanto faltou de saldo pra reservar a linha inteira. Fica em
    wms_pedido_itens.falta_un (nao em motivo_pendencia, que e de falha de
    CATALOGO): sao coisas diferentes -- a falta some sozinha quando o
    galpao enderecar, a de catalogo exige corrigir cadastro.
    """
    conn.execute("UPDATE wms_pedido_itens SET falta_un = ? WHERE id = ?",
                 (round(float(faltou or 0), 3), int(item_id)))


def registrar_pedido(conn, pedido: dict, itens: list[dict]) -> int:
    """
    Cria ou atualiza o espelho do pedido e as linhas de item, ja resolvendo
    produto e unidade. Idempotente por id_stokki + linha.

    Linha que existia numa chamada anterior e nao vem em `itens` desta vez
    (o pedido foi editado na Stokki e a linha saiu) tem suas reservas ATIVAS
    canceladas E ganha `removido_em` preenchido -- a linha em si NAO e
    apagada de wms_pedido_itens, o historico importa; so a reserva e
    liberada, pra nao esconder estoque de um item que nao existe mais no
    pedido.

    O `removido_em` e o que impede o achado critico da revisao (22/09): sem
    ele, o reservar_pedido logo em seguida (a rotina de lote chama os dois
    em sequencia) iterava TODOS os itens do pedido, inclusive o que acabara
    de sair, e ressuscitava a reserva -- a mercadoria virava SAIDA na
    expedicao sem estar no pedido. Linha que volta pro pedido tem o
    removido_em limpo no upsert abaixo e volta a ser reservavel.
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
                produto_id = excluded.produto_id, motivo_pendencia = excluded.motivo_pendencia,
                removido_em = ''""",
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
        conn.execute(
            "UPDATE wms_pedido_itens SET removido_em = ?, falta_un = 0 WHERE id = ? AND removido_em = ''",
            (agora, it["id"]))
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

    Linha marcada com `removido_em` (saiu do pedido na Stokki) NAO entra:
    a linha fica no historico, mas nao pode voltar a reservar estoque
    (achado critico da revisao, 22/09 -- ver registrar_pedido).

    Pedido ja BAIXADO ou CANCELADO tambem nao e re-reservado: depois da
    baixa as reservas estao CONSUMIDAS e uma rodada seguinte criava
    reserva ATIVA nova e devolvia o pedido pra RESERVADO, travando
    estoque que ja saiu do galpao.

    Item que nao resolve produto ou unidade nao vira reserva -- vira
    pendencia. Falta de saldo nunca levanta erro -- vira pendencia e o
    pedido fica PARCIAL (decisao do Hugo, 21/09: avisar, nunca bloquear);
    a falta fica gravada em wms_pedido_itens.falta_un, pra tela de
    pendencias mostrar (some sozinha quando o galpao enderecar).

    A comparacao que decide RESERVADO x PARCIAL arredonda a soma das
    reservas a 3 casas (ROUND(...,3) no SQL): cada alocacao do FEFO ja vem
    arredondada individualmente, mas a SOMA de varias delas pode acumular
    residuo de ponto flutuante (ex.: 0.1 + 0.2 + 0.3 = 0.6000000000000001)
    e marcar como PARCIAL um pedido que na pratica esta 100% reservado.
    """
    agora = wms.agora()
    pendencias = []
    criadas = 0
    pedido = conn.execute("SELECT * FROM wms_pedidos WHERE id = ?", (pedido_id,)).fetchone()
    if pedido and pedido["estado_reserva"] in ("BAIXADO", "CANCELADO"):
        return {"estado": pedido["estado_reserva"], "reservas": 0, "pendencias": [], "ignorado": True}
    itens = conn.execute(
        "SELECT * FROM wms_pedido_itens WHERE pedido_id = ? AND removido_em = '' ORDER BY linha",
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
            _gravar_falta(conn, item["id"], 0)  # ja esta reservado inteiro
            continue
        if not item["produto_id"] or item["qtd_un"] is None:
            # pendencia de CATALOGO (motivo_pendencia) -- nao e falta de saldo
            _gravar_falta(conn, item["id"], 0)
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
        _gravar_falta(conn, item["id"], faltou)
        if faltou > 0:
            pendencias.append(
                f"linha {item['linha']} ({item['descricao']}): faltaram {faltou:g} UN em estoque")

    total_itens = len(itens)
    resolvidos = conn.execute("""
        SELECT COUNT(*) n FROM wms_pedido_itens i
         WHERE i.pedido_id = ? AND i.removido_em = '' AND i.qtd_un IS NOT NULL
           AND ROUND(i.qtd_un, 3) <= ROUND(COALESCE((SELECT SUM(r.quantidade_un) FROM wms_reservas r
                                      WHERE r.item_id = i.id AND r.estado IN ('ATIVA','CONSUMIDA')), 0), 3)
        """, (pedido_id,)).fetchone()["n"]
    estado = "RESERVADO" if total_itens and resolvidos == total_itens else "PARCIAL"
    conn.execute("UPDATE wms_pedidos SET estado_reserva = ?, atualizado_em = ? WHERE id = ?",
                 (estado, agora, pedido_id))
    return {"estado": estado, "reservas": criadas, "pendencias": pendencias}


def cancelar_reservas(conn, pedido_id: int, motivo: str = "") -> int:
    """
    Libera as reservas ATIVAS do pedido e marca o pedido como CANCELADO.
    Chamada pela rotina de lote quando o pedido some da Stokki ou aparece
    cancelado la (spec 7.3 item 6) -- sem isso a reserva trava o
    disponivel pra sempre.

    O motivo fica gravado em wms_pedidos.motivo_cancelamento: seis meses
    depois, "por que este pedido esta CANCELADO" tem resposta.
    """
    agora = wms.agora()
    cur = conn.execute(
        "UPDATE wms_reservas SET estado = 'CANCELADA', atualizado_em = ? WHERE pedido_id = ? AND estado = 'ATIVA'",
        (agora, pedido_id))
    conn.execute(
        "UPDATE wms_pedidos SET estado_reserva = 'CANCELADO', motivo_cancelamento = ?, atualizado_em = ? "
        "WHERE id = ?", (str(motivo or "")[:300], agora, pedido_id))
    return cur.rowcount


def _id_stokki_do_codigo(codigo_ps: str) -> int | None:
    """
    Tira o id numerico do codigo do pedido. A VUUPT devolve '#PS-39751',
    'PS-39751' ou '#PS-39751-R2' (reentrega) -- todos sao o mesmo pedido.
    """
    m = re.search(r"PS-?(\d+)", str(codigo_ps or "").upper())
    return int(m.group(1)) if m else None


def baixar_por_expedicao(conn, codigo_ps: str, operador: dict | None = None) -> dict:
    """
    Converte as reservas ATIVAS do pedido em SAIDA de estoque.

    Consome TODAS as reservas ATIVAS do pedido, seja origem FEFO (sugerida
    pelo sistema) ou MANUAL (o operador bipou outro lote) -- na hora de
    baixar nao importa quem escolheu o lote, so que a mercadoria saiu.

    O uuid do movimento e deterministico e derivado do ID DA RESERVA
    (ps-<id_stokki>-reserva-<id>) e o registrar_movimento ja e idempotente
    por uuid: rodar duas vezes nao baixa duas vezes, e `duplicado` quer
    dizer exatamente "esta reserva ja virou SAIDA" -- nunca "outra reserva
    da mesma linha ja virou", que era o bug C2 (ver o comentario no laco). Um erro numa reserva nao derruba as outras -- cada
    reserva e um movimento + um UPDATE que fecham a propria transacao
    (commit por reserva, nao um so no fim): assim a reserva seguinte sempre
    encontra a conexao limpa pro wms.registrar_movimento abrir a dele com
    BEGIN IMMEDIATE.

    O pedido so vira 'BAIXADO' quando, no fim, nao sobra nenhuma reserva
    ATIVA pra ele E nao houve erro nesta chamada -- checado consultando o
    banco de novo (nao so a lista `erros` em memoria, que pode divergir).
    Sobrando reserva ATIVA ou erro, fica 'PARCIAL' (o mesmo estado que o
    resto do projeto usa pra "nao esta inteiro, olhe isto"): marcar
    'BAIXADO' um pedido que baixou so uma parte esconderia a reserva orfa
    que continua travando o disponivel. Uma chamada seguinte, depois do
    problema resolvido, completa a baixa das reservas que sobraram ATIVAS
    e o pedido termina 'BAIXADO' normalmente.

    A baixa aceita deixar saldo NEGATIVO (permitir_negativo=True): a
    mercadoria ja saiu fisicamente e recusar deixaria o estoque mais
    errado. Mas negativo nunca passa calado -- volta em `negativos` e fica
    gravado em wms_pedidos.saldo_negativo, pra tela da equipe mostrar.
    Negativo sempre quer dizer entrada nao registrada ou contagem errada.

    ATENCAO -- esta funcao FAZ COMMIT. Da propria baixa (por reserva) e de
    qualquer escrita pendente na conexao antes de comecar (ex.: um
    registrar_pedido/reservar_pedido chamado antes, na mesma conexao, sem
    commit). E proposital -- e como o resto da rotina de lote deste projeto
    trabalha, pra nao segurar lock no dados.db (compartilhado com o painel,
    ja derrubou o servico com "database is locked"). Por causa disso, NUNCA
    chame esta funcao dentro de uma transacao do chamador que precise poder
    desfazer tudo: ela nao pode ser embrulhada, so pode fechar.
    """
    id_stokki = _id_stokki_do_codigo(codigo_ps)
    if not id_stokki:
        return {"pedido_id": None, "baixas": 0, "ja_baixado": False, "negativos": [],
                "erros": [f"codigo de pedido nao reconhecido: {codigo_ps!r}"]}
    pedido = conn.execute("SELECT * FROM wms_pedidos WHERE id_stokki = ?", (id_stokki,)).fetchone()
    if not pedido:
        return {"pedido_id": None, "baixas": 0, "ja_baixado": False, "erros": [], "negativos": []}

    reservas = conn.execute("""
        SELECT r.*, i.linha FROM wms_reservas r
          JOIN wms_pedido_itens i ON i.id = r.item_id
         WHERE r.pedido_id = ? AND r.estado = 'ATIVA'
         ORDER BY i.linha, r.id""", (pedido["id"],)).fetchall()
    if not reservas:
        ja = pedido["estado_reserva"] == "BAIXADO"
        return {"pedido_id": pedido["id"], "baixas": 0, "ja_baixado": ja, "erros": [], "negativos": []}

    # Fecha qualquer transacao pendente de uma escrita anterior nesta mesma
    # conexao (ex.: registrar_pedido/reservar_pedido chamados antes sem
    # commit). wms.registrar_movimento abre a propria transacao com BEGIN
    # IMMEDIATE e nao aceita rodar dentro de uma ja aberta.
    conn.commit()

    baixas, erros, negativos = 0, [], []
    for r in reservas:
        # O uuid vem do ID DA RESERVA, que e estavel pra sempre -- nunca
        # de um contador da chamada (achado critico C2 da revisao, 23/09):
        # o uuid antigo (ps-<id>-item-<linha>-<n>) contava so as reservas
        # ATIVAS DAQUELA chamada. Linha partida pelo FEFO em dois lotes com
        # a primeira baixa OK e a segunda estourando fazia a sobrevivente
        # virar n=1 na rodada seguinte, colidir com o uuid ja gravado,
        # voltar duplicado=True e ser marcada CONSUMIDA apontando pro
        # movimento DA OUTRA -- mercadoria saindo do galpao sem SAIDA
        # nenhuma, em silencio (pedido BAIXADO, sem pendencia). Com o id da
        # reserva, duplicado=True quer dizer exatamente "esta reserva ja
        # foi baixada", e ai marcar CONSUMIDA e a coisa certa.
        uuid = f"ps-{id_stokki}-reserva-{r['id']}"
        try:
            mov = wms.registrar_movimento(
                conn, tipo="SAIDA", produto_id=r["produto_id"], quantidade=r["quantidade_un"],
                lote=r["lote"], validade=r["validade"] or None, origem=r["posicao"],
                operador=operador, uuid=uuid,
                observacao=f"Baixa automatica da expedicao do {pedido['codigo_ps']}",
                permitir_negativo=True)
            # permitir_negativo=True e decisao consciente: a mercadoria ja
            # saiu fisicamente, recusar a baixa deixaria o estoque MAIS
            # errado. Mas saldo negativo nao pode passar em silencio -- e
            # sempre sinal de entrada nao registrada ou contagem errada.
            for posicao, saldo in (mov.get("saldos") or {}).items():
                if saldo < 0:
                    negativos.append(
                        f"{posicao} ficou com {saldo:g} UN de {r['lote'] or '(sem lote)'} "
                        f"(linha {r['linha']}) -- entrada faltando no WMS")
            conn.execute(
                "UPDATE wms_reservas SET estado = 'CONSUMIDA', movimento_uuid = ?, atualizado_em = ? WHERE id = ?",
                (mov["uuid"], wms.agora(), r["id"]))
            # Commit por reserva (nao um so no fim): fecha a transacao deste
            # par movimento+UPDATE agora, pra reserva seguinte encontrar a
            # conexao limpa. Sem isso, o UPDATE acima deixa uma transacao
            # implicita aberta e o BEGIN IMMEDIATE da proxima reserva estoura
            # "cannot start a transaction within a transaction" -- o except
            # engolia o erro e o rollback do registrar_movimento desfazia ate
            # o UPDATE da reserva anterior, que ja tinha saida gravada.
            conn.commit()
            if not mov.get("duplicado"):
                baixas += 1
        except Exception as e:  # noqa: BLE001 -- uma reserva ruim nao derruba a expedicao
            erros.append(f"linha {r['linha']}: {e}")

    # Confere no banco (nao so na lista `erros` em memoria, que pode
    # divergir) se sobrou reserva ATIVA pra este pedido. So marca BAIXADO
    # quando a baixa saiu inteira; senao fica PARCIAL, pra nao mascarar
    # uma reserva orfa como se o estoque estivesse certo.
    restantes_ativas = conn.execute(
        "SELECT COUNT(*) n FROM wms_reservas WHERE pedido_id = ? AND estado = 'ATIVA'",
        (pedido["id"],)).fetchone()["n"]
    novo_estado = "BAIXADO" if not restantes_ativas and not erros else "PARCIAL"
    conn.execute("UPDATE wms_pedidos SET estado_reserva = ?, atualizado_em = ? WHERE id = ?",
                 (novo_estado, wms.agora(), pedido["id"]))
    if negativos:
        conn.execute("UPDATE wms_pedidos SET saldo_negativo = ? WHERE id = ?",
                     (" | ".join(negativos)[:500], pedido["id"]))
    conn.commit()
    return {"pedido_id": pedido["id"], "baixas": baixas, "ja_baixado": False,
            "erros": erros, "negativos": negativos}


def pendencias(conn, limite: int = 50) -> list[dict]:
    """
    Itens que nao viraram reserva inteira. Dois tipos, de proposito
    distinguiveis (`tipo_pendencia`):

      CATALOGO -- produto ou unidade nao resolvidos (motivo_pendencia).
                  So some corrigindo cadastro.
      SALDO    -- resolveu o produto, mas faltou estoque (falta_un > 0).
                  Some sozinho quando o galpao enderecar a mercadoria.

    Sem o tipo SALDO a tela mentia: na primeira semana, com o galpao ainda
    sem enderecar nada, TODO pedido fica PARCIAL e a tela dizia "Nenhuma
    pendencia -- todos os itens resolveram".

    Junta com o pedido pra dar contexto (codigo, embarcador, situacao), e
    deixa de fora linha removida do pedido e pedido ja CANCELADO ou
    BAIXADO (a pendencia deles nao importa mais).

    Usado pela rotina de lote (sincronizar_pedidos_wms.py) pra logar o que
    precisa de atencao manual, e pela tela do painel pra listar.
    """
    rows = conn.execute("""
        SELECT i.*, p.codigo_ps, p.embarcador, p.situacao, p.estado_reserva,
               CASE WHEN i.motivo_pendencia <> '' THEN 'CATALOGO' ELSE 'SALDO' END AS tipo_pendencia
          FROM wms_pedido_itens i JOIN wms_pedidos p ON p.id = i.pedido_id
         WHERE (i.motivo_pendencia <> '' OR i.falta_un > 0)
           AND i.removido_em = ''
           AND p.estado_reserva NOT IN ('CANCELADO', 'BAIXADO')
         ORDER BY p.id DESC, i.linha LIMIT ?""", (int(limite),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["motivo"] = d["motivo_pendencia"] or (
            f"faltaram {float(d['falta_un']):g} UN em estoque -- aguardando enderecamento no galpao")
        out.append(d)
    return out


def reservas_antigas(conn, dias: int = RESERVA_ANTIGA_DIAS, limite: int = 50) -> list[dict]:
    """
    Pedidos cuja reserva ATIVA mais velha passou de `dias` dias.

    Reserva so nasce pra ser consumida pela baixa da expedicao (dias) ou
    liberada quando o pedido some/cancela. Uma que fica ATIVA muito tempo
    quer dizer que alguma coisa NAO fechou: o pedido saiu por fora da
    rota (retirada no galpao, redespacho, transportadora propria) e a
    baixa nunca foi chamada, a baixa estourou, ou o pedido virou outra
    coisa na Stokki. Enquanto isso a mercadoria some do disponivel e faz
    pedido novo nascer PARCIAL por falta que nao existe.

    A varredura de sincronizar_pedidos_wms.py resolve o caso comum
    sozinha; esta lista e o que revela o que escapou dela -- e o que a
    equipe usa pra decidir se libera a reserva na mao.

    Uma linha por PEDIDO (nao por reserva), com a idade em dias e o que
    esta preso (item, posicao, lote, quantidade) -- "PS-123 ha 6 dias"
    sem dizer o que esta segurando nao ajuda ninguem a agir.
    """
    corte = (datetime.now() - timedelta(days=int(dias))).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT p.id AS pedido_id, p.codigo_ps, p.embarcador, p.situacao, p.estado_reserva,
               MIN(r.criado_em) AS reservada_em, COUNT(*) AS reservas,
               ROUND(SUM(r.quantidade_un), 3) AS total_un
          FROM wms_reservas r
          JOIN wms_pedidos p ON p.id = r.pedido_id
         WHERE r.estado = 'ATIVA'
         GROUP BY p.id
        HAVING MIN(r.criado_em) < ?
         ORDER BY reservada_em LIMIT ?""", (corte, int(limite))).fetchall()
    agora = datetime.now()
    out = []
    for r in rows:
        d = dict(r)
        try:
            idade = agora - datetime.strptime(d["reservada_em"], "%Y-%m-%d %H:%M:%S")
            d["dias"] = round(idade.total_seconds() / 86400.0, 1)
        except (TypeError, ValueError):  # criado_em em formato inesperado
            d["dias"] = None
        d["itens"] = [dict(x) for x in conn.execute("""
            SELECT i.linha, i.descricao, i.sku, r.posicao, r.lote, r.validade, r.quantidade_un
              FROM wms_reservas r
              JOIN wms_pedido_itens i ON i.id = r.item_id
             WHERE r.pedido_id = ? AND r.estado = 'ATIVA'
             ORDER BY i.linha, r.id""", (d["pedido_id"],))]
        out.append(d)
    return out


def registrar_recebimento(conn, recebimento: dict, itens: list[dict]) -> int:
    """
    Cria ou atualiza o espelho de um recebimento (entrada esperada) e as
    linhas de item, ja resolvendo produto e unidade -- mesmo padrao de
    registrar_pedido, so que aqui nao ha reserva nenhuma (nao mexe em
    saldo, so cria a lista do que o operador vai enderecar no celular).

    Idempotente por id_stokki + linha. O estado do recebimento (ESPERADO
    | ENDERECADO) so nasce ESPERADO na criacao -- rodadas seguintes NUNCA
    voltam um recebimento ja ENDERECADO pra ESPERADO, so atualizam os
    campos de cabecalho (situacao/chegada podem mudar na Stokki antes do
    operador terminar de enderecar).

    Decisao do Hugo (23/09/2026): sem pre-preenchimento vindo da Stokki --
    da Stokki so entra a quantidade. Lote e validade o operador digita na
    tela do celular, lendo a caixa fisica (fonte de verdade).

    `stkkc_id` (o #stkkc-<id> do embarcador na propria linha da Stokki) e
    gravado junto: e por ele que o relatorio de faltas acha o contato do
    embarcador nas preferencias de notificacao. Sem ele o relatorio teria
    que adivinhar pelo nome -- e mandar falta de mercadoria pro cliente
    errado e o tipo de erro que nao se conserta depois.
    """
    id_stokki = int(recebimento["id_stokki"])
    agora = wms.agora()

    row = conn.execute("SELECT id FROM wms_recebimentos WHERE id_stokki = ?", (id_stokki,)).fetchone()
    if row:
        recebimento_id = row["id"]
        conn.execute(
            "UPDATE wms_recebimentos SET codigo = ?, embarcador = ?, stkkc_id = ?, situacao = ?, "
            "chegada = ?, atualizado_em = ? WHERE id = ?",
            (recebimento.get("codigo", ""), recebimento.get("embarcador", ""),
             str(recebimento.get("stkkc_id") or ""), recebimento.get("situacao", ""),
             recebimento.get("chegada", ""), agora, recebimento_id))
    else:
        cur = conn.execute("""
            INSERT INTO wms_recebimentos (id_stokki, codigo, embarcador, stkkc_id, situacao, chegada,
                                          estado, lido_em, atualizado_em)
            VALUES (?,?,?,?,?,?,'ESPERADO',?,?)""",
            (id_stokki, recebimento.get("codigo", ""), recebimento.get("embarcador", ""),
             str(recebimento.get("stkkc_id") or ""), recebimento.get("situacao", ""),
             recebimento.get("chegada", ""), agora, agora))
        recebimento_id = cur.lastrowid

    for item in itens:
        r = resolver_item(conn, item)
        conn.execute("""
            INSERT INTO wms_recebimento_itens (recebimento_id, linha, sku, ean_linha, descricao,
                                               qtd_embalagem, qtd_un, produto_id, motivo_pendencia)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(recebimento_id, linha) DO UPDATE SET
                sku = excluded.sku, ean_linha = excluded.ean_linha, descricao = excluded.descricao,
                qtd_embalagem = excluded.qtd_embalagem, qtd_un = excluded.qtd_un,
                produto_id = excluded.produto_id, motivo_pendencia = excluded.motivo_pendencia""",
            (recebimento_id, int(item["linha"]), item.get("sku", ""), item.get("ean_linha", ""),
             item.get("descricao", ""), float(item["qtd_embalagem"]), r["qtd_un"], r["produto_id"],
             r["motivo_pendencia"]))

    return recebimento_id


def itens_do_recebimento(conn, recebimento_id: int) -> list[dict]:
    """
    Linhas de um recebimento pra tela do operador. A unidade vem do
    produto do catalogo -- wms_recebimento_itens nao tem essa coluna, e a
    tela mostrava "undefined" no rotulo da quantidade.
    """
    rows = conn.execute("""
        SELECT i.*, COALESCE(p.unidade, 'UN') AS unidade
          FROM wms_recebimento_itens i
          LEFT JOIN wms_produtos p ON p.id = i.produto_id
         WHERE i.recebimento_id = ? ORDER BY i.linha""", (int(recebimento_id),)).fetchall()
    return [dict(r) for r in rows]


def trocar_lote_reserva(conn, reserva_id: int, posicao: str, lote: str, validade: str | None) -> dict:
    """
    O operador bipou a etiqueta do lote que pegou (spec 7.4).

    Bipou o MESMO lote/posicao/validade da reserva -> e confirmacao, nao
    troca: devolve a reserva como esta, sem erro e sem reserva nova. Era o
    achado mais bobo e mais grave da tela: disponivel_por_lote ja desconta
    a propria reserva, entao confirmar o lote sugerido -- a acao mais
    natural do operador -- dava "tem 0 UN disponivel".

    Bipou outro lote -> cancela a sugerida e cria uma MANUAL no lote
    bipado, se houver disponivel. O disponivel do lote de destino nao
    conta a reserva que esta sendo substituida (ela sai no mesmo ato).
    """
    r = conn.execute("SELECT * FROM wms_reservas WHERE id = ?", (int(reserva_id),)).fetchone()
    if not r:
        raise wms.ErroWMS("Reserva nao encontrada.")
    if r["estado"] != "ATIVA":
        raise wms.ErroWMS(f"Reserva ja esta {r['estado'].lower()}.")
    posicao = wms.normalizar_codigo(posicao)
    lote = " ".join(str(lote or "").split()).upper()
    validade = wms._validar_validade(validade) or ""

    mesma_chave = (r["posicao"] == posicao and r["lote"] == lote and (r["validade"] or "") == validade)
    if mesma_chave:
        d = dict(r)
        d["confirmada"] = True
        return d

    alvo = [d for d in disponivel_por_lote(conn, r["produto_id"])
            if d["posicao"] == posicao and d["lote"] == lote and d["validade"] == validade]
    disponivel = alvo[0]["disponivel"] if alvo else 0
    if disponivel < r["quantidade_un"]:
        raise wms.ErroWMS(
            f"Lote {lote or '(sem lote)'} em {posicao} tem {disponivel:g} UN disponivel, "
            f"menos que os {r['quantidade_un']:g} UN da reserva.")
    agora = wms.agora()
    conn.execute("UPDATE wms_reservas SET estado='CANCELADA', atualizado_em=? WHERE id=?", (agora, r["id"]))
    cur = conn.execute("""
        INSERT INTO wms_reservas (pedido_id, item_id, produto_id, posicao, lote, validade,
                                  quantidade_un, estado, origem, criado_em, atualizado_em)
        VALUES (?,?,?,?,?,?,?,'ATIVA','MANUAL',?,?)""",
        (r["pedido_id"], r["item_id"], r["produto_id"], posicao, lote, validade,
         r["quantidade_un"], agora, agora))
    conn.commit()
    nova = dict(conn.execute("SELECT * FROM wms_reservas WHERE id = ?", (cur.lastrowid,)).fetchone())
    nova["confirmada"] = False
    return nova


def contabilizar_enderecamento(conn, recebimento_id: int, item_id: int, qtd: float,
                               uuid: str = "") -> dict:
    """
    Contabiliza quanto de uma linha do recebimento ja foi enderecado.
    Chamada DEPOIS que a ENTRADA em si foi aceita por registrar_movimento
    -- aqui nao se move estoque nenhum, so se anda a barra do recebimento.

    Idempotente pelo mesmo `uuid` do movimento de ENTRADA do aparelho: a
    contabilizacao e uma segunda chamada HTTP e o celular do galpao perde
    resposta a toda hora. Sem isso, "registrar entrada" repetido com o
    movimento ja gravado (que registrar_movimento devolve como duplicado)
    somava de novo no qtd_enderecada e fechava o recebimento como
    ENDERECADO com mercadoria faltando.

    O incremento e feito no proprio SQL (qtd_enderecada = qtd_enderecada +
    ?), nao em read-modify-write no Python: dois operadores enderecando a
    mesma linha ao mesmo tempo perdiam um dos incrementos.
    """
    item = conn.execute("SELECT * FROM wms_recebimento_itens WHERE id = ? AND recebimento_id = ?",
                        (int(item_id), int(recebimento_id))).fetchone()
    if not item:
        raise wms.ErroWMS("Item do recebimento nao encontrado.")
    qtd = round(float(qtd or 0), 3)
    uuid = str(uuid or "").strip()
    duplicado = False
    if uuid:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wms_recebimento_enderecamentos (uuid, recebimento_id, item_id, "
            "quantidade, criado_em) VALUES (?,?,?,?,?)",
            (uuid, int(recebimento_id), int(item_id), qtd, wms.agora()))
        duplicado = cur.rowcount == 0

    if not duplicado:
        conn.execute(
            "UPDATE wms_recebimento_itens SET qtd_enderecada = ROUND(qtd_enderecada + ?, 3) WHERE id = ?",
            (qtd, int(item_id)))
    # "Fecha sozinho" e exatamente "nao ha falta nenhuma": e a MESMA funcao
    # que decide o que vai no relatorio de faltas do cliente, de proposito.
    # Enquanto eram duas comparacoes iguais escritas em lugares diferentes
    # (uma em SQL aqui, outra em Python la), qualquer mexida numa delas
    # criava em silencio o recebimento que nao fecha automatico e tambem nao
    # tem falta pra reportar -- o operador ficaria sem saida nenhuma.
    if not faltas_do_recebimento(conn, recebimento_id):
        # So o ESPERADO vira ENDERECADO: recebimento ja encerrado com
        # DIVERGENCIA (relatorio enviado ao cliente) nao pode ser apagado
        # por um enderecamento atrasado.
        conn.execute("UPDATE wms_recebimentos SET estado = 'ENDERECADO', atualizado_em = ? "
                     "WHERE id = ? AND estado = 'ESPERADO'",
                     (wms.agora(), int(recebimento_id)))
    conn.commit()
    return {
        "duplicado": duplicado,
        "item": dict(conn.execute("SELECT * FROM wms_recebimento_itens WHERE id = ?", (int(item_id),)).fetchone()),
        "recebimento": dict(conn.execute("SELECT * FROM wms_recebimentos WHERE id = ?",
                                         (int(recebimento_id),)).fetchone()),
    }


def faltas_do_recebimento(conn, recebimento_id: int) -> list[dict]:
    """
    O que foi anunciado e nao foi enderecado, linha a linha.

    Entra so linha RESOLVIDA (produto identificado no catalogo) cujo
    enderecado ficou abaixo do esperado. Linha que virou pendencia de
    catalogo NAO entra de proposito: o operador nao conseguiu enderecar
    por problema NOSSO (produto sem cadastro), e chamar isso de "faltou"
    num relatorio que vai pro cliente seria acusar o transportador dele de
    uma falta que nunca houve. Essa linha continua visivel na tela do
    galpao como pendencia, que e onde ela se resolve.

    E a MESMA funcao que contabilizar_enderecamento usa pra decidir se o
    recebimento fecha sozinho ("fecha" = esta lista vazia), pra nao existir
    dois criterios espelhados que possam divergir numa mexida futura.
    """
    rows = conn.execute("""
        SELECT i.*, COALESCE(p.unidade, 'UN') AS unidade
          FROM wms_recebimento_itens i
          LEFT JOIN wms_produtos p ON p.id = i.produto_id
         WHERE i.recebimento_id = ? AND i.produto_id IS NOT NULL AND i.qtd_un IS NOT NULL
         ORDER BY i.linha""", (int(recebimento_id),)).fetchall()
    faltas = []
    for r in rows:
        falta = round(round(float(r["qtd_un"]), 3) - round(float(r["qtd_enderecada"] or 0), 3), 3)
        if falta <= 0:
            continue
        d = dict(r)
        d["falta_un"] = falta
        faltas.append(d)
    return faltas


def faltas_congeladas(conn, recebimento_id: int) -> list[dict]:
    """
    A falta que FICOU GRAVADA no encerramento com divergencia (falta_un),
    nao a recalculada. E o que o relatorio manda pro cliente e o que o
    reenvio tem que repetir palavra por palavra meses depois, mesmo que a
    quantidade anunciada mude na Stokki.
    """
    return [dict(r) for r in conn.execute("""
        SELECT i.*, COALESCE(p.unidade, 'UN') AS unidade
          FROM wms_recebimento_itens i
          LEFT JOIN wms_produtos p ON p.id = i.produto_id
         WHERE i.recebimento_id = ? AND i.falta_un > 0 ORDER BY i.linha""", (int(recebimento_id),))]


def encerrar_com_divergencia(conn, recebimento_id: int, observacao: str = "",
                             operador: dict | None = None) -> dict:
    """
    O operador terminou a descarga e faltou mercadoria (carga parcial,
    avaria, item que nao veio). Fecha o recebimento assumindo a falta.

    Sem isto, o recebimento que recebeu menos do que foi anunciado nunca
    alcanca o esperado, nunca fecha sozinho e fica pra sempre na lista do
    operador -- sem ninguem saber se e trabalho pendente ou se simplesmente
    nao veio.

    NAO MEXE EM ESTOQUE, de proposito: o que chegou ja entrou pelas
    ENTRADAs do enderecamento, e o que faltou nunca existiu. Nenhuma linha
    aqui escreve em wms_saldos ou wms_movimentos.

    A falta de cada linha e CONGELADA em wms_recebimento_itens.falta_un: o
    relatorio que vai pro cliente precisa continuar dizendo a mesma coisa
    seis meses depois, mesmo que a Stokki mude a quantidade anunciada
    depois. `observacao` e a diferenca entre "chegou avariado" e "nao
    veio", que nao e a mesma conversa pra quem recebe o relatorio.
    """
    rec = conn.execute("SELECT * FROM wms_recebimentos WHERE id = ?", (int(recebimento_id),)).fetchone()
    if not rec:
        raise wms.ErroWMS("Recebimento nao encontrado.")
    if rec["estado"] != "ESPERADO":
        raise wms.ErroWMS(f"Recebimento ja encerrado ({rec['estado']}).")
    faltas = faltas_do_recebimento(conn, recebimento_id)
    if not faltas:
        raise wms.ErroWMS(
            "Nao ha falta nenhuma neste recebimento -- ele fecha sozinho quando a ultima linha bater.")

    agora = wms.agora()
    for f in faltas:
        conn.execute("UPDATE wms_recebimento_itens SET falta_un = ? WHERE id = ?",
                     (f["falta_un"], f["id"]))
    conn.execute(
        "UPDATE wms_recebimentos SET estado = 'DIVERGENCIA', observacao_divergencia = ?, "
        "encerrado_em = ?, encerrado_por = ?, atualizado_em = ? WHERE id = ?",
        (" ".join(str(observacao or "").split())[:500], agora,
         str((operador or {}).get("nome") or "")[:60], agora, int(recebimento_id)))
    conn.commit()
    return {"recebimento": dict(conn.execute(
        "SELECT * FROM wms_recebimentos WHERE id = ?", (int(recebimento_id),)).fetchone()),
        "faltas": faltas}


def listar_pedidos_wms(conn, estado: str | None = None, limite: int = 50) -> list[dict]:
    """Pedidos espelhados, com contagem de itens e reservas -- pra tela."""
    sql = """
        SELECT p.*,
               (SELECT COUNT(*) FROM wms_pedido_itens i WHERE i.pedido_id = p.id) AS itens,
               (SELECT COUNT(*) FROM wms_reservas r WHERE r.pedido_id = p.id AND r.estado='ATIVA') AS reservas_ativas,
               (SELECT COUNT(*) FROM wms_pedido_itens i WHERE i.pedido_id = p.id AND i.motivo_pendencia <> '') AS pendencias
          FROM wms_pedidos p {onde} ORDER BY p.id DESC LIMIT ?"""
    if estado:
        rows = conn.execute(sql.format(onde="WHERE p.estado_reserva = ?"), (estado, int(limite))).fetchall()
    else:
        rows = conn.execute(sql.format(onde=""), (int(limite),)).fetchall()
    return [dict(r) for r in rows]


def separacao_do_pedido(conn, pedido_id: int) -> list[dict]:
    """Lista de separacao: uma linha por reserva, com posicao, lote e validade."""
    rows = conn.execute("""
        SELECT r.*, i.linha, i.descricao, i.sku, i.qtd_un AS qtd_item,
               pr.unidade, pr.qtd_por_caixa
          FROM wms_reservas r
          JOIN wms_pedido_itens i ON i.id = r.item_id
          JOIN wms_produtos pr ON pr.id = r.produto_id
         WHERE r.pedido_id = ? AND r.estado = 'ATIVA'
         ORDER BY r.posicao, i.linha""", (int(pedido_id),)).fetchall()
    return [dict(r) for r in rows]
