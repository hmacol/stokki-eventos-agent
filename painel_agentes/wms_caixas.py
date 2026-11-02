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
import logging
import math
import sqlite3
from pathlib import Path

import wms
import wms_fila_impressao
import wms_pedidos
from wms import ErroWMS

SITUACOES_CAIXA = ("IMPRESSA", "EM_ESTOQUE", "ABERTA", "EXPEDIDA", "CANCELADA")
MAX_CAIXAS = 200
MAX_QTD_UN = 100000

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
    except (TypeError, ValueError, OverflowError):
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
        try:
            qtd_un = float(qtd_un)
        except (TypeError, ValueError):
            raise ErroWMS("Quantidade por caixa invalida.")
        if not math.isfinite(qtd_un) or qtd_un <= 0 or qtd_un > MAX_QTD_UN:
            raise ErroWMS(f"Quantidade por caixa deve ser maior que 0 e no maximo {MAX_QTD_UN}.")
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


# ---------------------------------------------------------------------------
# Ativar, mover e cancelar
# ---------------------------------------------------------------------------
# Regra de ouro: todo caminho que mexe em saldo passa por wms.registrar_movimento
# com uuid deterministico (ativar/cancelar) ou do aparelho (mover). O movimento
# faz o proprio commit, entao nunca se chama com transacao aberta. Depois dele,
# o UPDATE da caixa + evento vao numa transacao curta; se a queda for entre os
# dois, repetir a chamada acha o movimento (duplicado) e so completa a caixa.

def _op(operador: dict | None) -> tuple[str, str]:
    operador = operador or {}
    return str(operador.get("id") or ""), str(operador.get("nome") or "")


def _fechar_transacao(conn) -> None:
    if conn.in_transaction:
        conn.commit()


def _caixa_ou_erro(conn, serial: str) -> dict:
    serial = str(serial or "").strip().upper()
    caixa = obter_caixa(conn, serial)
    if not caixa:
        raise ErroWMS(f"Caixa {serial} nao existe.")
    return caixa


def _mudar_caixa(conn, serial: str, *, de_situacoes: tuple, para: str, posicao: str | None,
                 evento_uuid: str, movimento_uuid: str | None, operador: dict | None,
                 observacao: str = "", manter_posicao: bool = False,
                 posicao_antes: str | None = None) -> bool:
    """UPDATE da caixa + evento numa transacao curta. O evento so entra se a caixa
    mudou de fato (rowcount 1). posicao_antes trava o UPDATE na posicao esperada.
    Devolve True se mudou."""
    op_id, op_nome = _op(operador)
    marcas = ",".join("?" * len(de_situacoes))
    trava, args_trava = "", ()
    if posicao_antes is not None:
        trava, args_trava = " AND posicao = ?", (posicao_antes,)
    conn.execute("BEGIN IMMEDIATE")
    try:
        atual = conn.execute("SELECT situacao, posicao FROM wms_caixas WHERE serial = ?", (serial,)).fetchone()
        ts = wms.agora()
        if manter_posicao:
            cur = conn.execute(f"UPDATE wms_caixas SET situacao = ?, atualizado_em = ? "
                               f"WHERE serial = ? AND situacao IN ({marcas}){trava}",
                               (para, ts, serial, *de_situacoes, *args_trava))
        else:
            cur = conn.execute(f"UPDATE wms_caixas SET situacao = ?, posicao = ?, atualizado_em = ? "
                               f"WHERE serial = ? AND situacao IN ({marcas}){trava}",
                               (para, posicao, ts, serial, *de_situacoes, *args_trava))
        mudou = cur.rowcount == 1
        if mudou:
            conn.execute(
                "INSERT OR IGNORE INTO wms_caixa_eventos (uuid, serial, de, para, posicao, movimento_uuid, "
                "operador_id, operador_nome, observacao, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (evento_uuid, serial, atual["situacao"] if atual else None, para,
                 posicao if posicao is not None else (atual["posicao"] if atual else None),
                 movimento_uuid, op_id, op_nome, observacao[:500], ts))
        conn.commit()
        return mudou
    except Exception:
        conn.rollback()
        raise


def _contabilizar(conn, caixa: dict, recebimento_id, item_id, uuid: str) -> None:
    if recebimento_id is not None and item_id is not None:
        wms_pedidos.contabilizar_enderecamento(conn, recebimento_id, item_id, caixa["qtd_un"], uuid=uuid)


def _exigir_movimento_da_caixa(conn, uuid: str, caixa: dict, tipos: tuple):
    """Movimento ja gravado com o uuid da caixa so vale se for mesmo desta caixa."""
    ja = conn.execute("SELECT * FROM wms_movimentos WHERE uuid = ?", (uuid,)).fetchone()
    if not ja:
        return None
    if (ja["tipo"] not in tipos or ja["produto_id"] != caixa["produto_id"]
            or ja["lote"] != caixa["lote"] or (ja["validade"] or None) != (caixa["validade"] or None)
            or abs(float(ja["quantidade"]) - float(caixa["qtd_un"])) > 1e-9
            or caixa["serial"] not in str(ja["observacao"] or "")):
        raise ErroWMS(f"Movimento {uuid} nao pertence a caixa {caixa['serial']}.")
    return ja


def ativar_caixa(conn, serial: str, posicao: str, *, operador: dict | None = None,
                 recebimento_id: int | None = None, item_id: int | None = None,
                 dispositivo: str = "", criado_em: str | None = None) -> dict:
    """
    IMPRESSA -> EM_ESTOQUE na posicao. Sem recebimento soma por AJUSTE (nunca
    INVENTARIO, que grava a contagem absoluta da posicao); com recebimento
    soma por ENTRADA e anda a barra do recebimento com o mesmo uuid.
    """
    if (recebimento_id is None) != (item_id is None):
        raise ErroWMS("Informe recebimento e item juntos.")
    _fechar_transacao(conn)
    caixa = _caixa_ou_erro(conn, serial)
    serial = caixa["serial"]
    posicao = wms.normalizar_codigo(posicao)
    uuid = f"cx-{serial}-ativar"
    if caixa["situacao"] == "IMPRESSA":
        # So aqui um duplicado vira IMPRESSA -> EM_ESTOQUE; depois disso a quantidade da
        # caixa pode ter caido (consumo parcial) e o retry legitimo nao e comparavel.
        _exigir_movimento_da_caixa(conn, uuid, caixa, ("AJUSTE", "ENTRADA"))

    if recebimento_id is not None:
        item = conn.execute("SELECT produto_id FROM wms_recebimento_itens WHERE id = ? AND recebimento_id = ?",
                            (int(item_id), int(recebimento_id))).fetchone()
        if not item:
            raise ErroWMS("Item nao pertence a esse recebimento.")
        if item["produto_id"] != caixa["produto_id"]:
            raise ErroWMS(f"Item do recebimento e de outro produto; caixa {serial} nao pode entrar nele.")

    if caixa["situacao"] in ("EM_ESTOQUE", "ABERTA"):
        if caixa["posicao"] != posicao:
            raise ErroWMS(f"Caixa {serial} ja esta em {caixa['posicao']}. Use Mover.")
        # Resposta perdida: completa a barra do recebimento (idempotente pelo uuid),
        # mas so se a caixa entrou por ENTRADA; ativada no inventario nao conta como recebida.
        if recebimento_id is not None:
            ativ = conn.execute("SELECT tipo FROM wms_movimentos WHERE uuid = ?", (uuid,)).fetchone()
            if not ativ or ativ["tipo"] != "ENTRADA":
                raise ErroWMS(f"Caixa {serial} ja foi ativada no inventario; nao pode entrar neste recebimento.")
        _contabilizar(conn, caixa, recebimento_id, item_id, uuid)
        return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": True, "movimento_uuid": uuid}
    if caixa["situacao"] != "IMPRESSA":
        raise ErroWMS(f"Caixa {serial} esta {caixa['situacao']} e nao pode ser ativada.")

    comum = dict(produto_id=caixa["produto_id"], quantidade=caixa["qtd_un"], lote=caixa["lote"],
                 validade=caixa["validade"], destino=posicao, operador=operador, uuid=uuid,
                 dispositivo=dispositivo, criado_em=criado_em)
    if recebimento_id is not None:
        mov = wms.registrar_movimento(conn, tipo="ENTRADA", observacao=f"Caixa {serial}", **comum)
    else:
        mov = wms.registrar_movimento(conn, tipo="AJUSTE",
                                      observacao=f"Inventario de abertura - caixa {serial}", **comum)
    if recebimento_id is not None and mov.get("tipo") != "ENTRADA":
        raise ErroWMS(f"Caixa {serial} ja foi ativada no inventario; nao pode entrar neste recebimento.")
    # A posicao que vale e a do movimento gravado (pode ser de uma chamada anterior).
    pos_real = mov.get("posicao_destino") or posicao
    _mudar_caixa(conn, serial, de_situacoes=("IMPRESSA",), para="EM_ESTOQUE", posicao=pos_real,
                 evento_uuid=f"{uuid}-evento", movimento_uuid=uuid, operador=operador,
                 observacao="Ativada por recebimento" if recebimento_id is not None else "Ativada no inventario")
    _contabilizar(conn, caixa, recebimento_id, item_id, uuid)
    return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": bool(mov.get("duplicado")),
            "movimento_uuid": uuid}


def mover_caixa(conn, serial: str, destino: str, *, uuid: str, operador: dict | None = None) -> dict:
    """TRANSFERENCIA da caixa (leva o saldo junto). uuid do aparelho obrigatorio."""
    uuid = str(uuid or "").strip()
    if not uuid:
        raise ErroWMS("uuid do aparelho obrigatorio para mover.")
    _fechar_transacao(conn)
    caixa = _caixa_ou_erro(conn, serial)
    serial = caixa["serial"]
    destino = wms.normalizar_codigo(destino)
    evento_uuid = f"cx-{serial}-mover-{uuid}"

    if conn.execute("SELECT 1 FROM wms_caixa_eventos WHERE uuid = ?", (evento_uuid,)).fetchone():
        # Reenvio de um mover ja concluido: nada a fazer.
        return {"caixa": caixa, "duplicado": True, "movimento_uuid": uuid}
    if caixa["situacao"] not in ("EM_ESTOQUE", "ABERTA"):
        raise ErroWMS(f"Caixa {serial} esta {caixa['situacao']} e nao pode ser movida.")
    ja = conn.execute("SELECT * FROM wms_movimentos WHERE uuid = ?", (uuid,)).fetchone()
    if ja:
        # Movimento ja gravado (queda antes de atualizar a caixa). So vale se for desta caixa.
        if (ja["tipo"] != "TRANSFERENCIA" or ja["produto_id"] != caixa["produto_id"]
                or ja["lote"] != caixa["lote"] or (ja["validade"] or None) != (caixa["validade"] or None)
                or abs(float(ja["quantidade"]) - float(caixa["qtd_un"])) > 1e-9
                or ja["observacao"] != f"Caixa {serial}"):
            raise ErroWMS(f"uuid {uuid} ja foi usado por outro movimento; nao e desta caixa {serial}.")
        _mudar_caixa(conn, serial, de_situacoes=("EM_ESTOQUE", "ABERTA"), para=caixa["situacao"],
                     posicao=ja["posicao_destino"], evento_uuid=evento_uuid, movimento_uuid=uuid,
                     operador=operador, observacao=f"Moveu de {ja['posicao_origem']} para {ja['posicao_destino']}",
                     posicao_antes=ja["posicao_origem"])
        return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": True, "movimento_uuid": uuid}
    if caixa["posicao"] == destino:
        raise ErroWMS(f"Caixa {serial} ja esta em {destino}.")
    origem = caixa["posicao"]
    wms.registrar_movimento(conn, tipo="TRANSFERENCIA", produto_id=caixa["produto_id"],
                            quantidade=caixa["qtd_un"], lote=caixa["lote"], validade=caixa["validade"],
                            origem=origem, destino=destino, operador=operador, uuid=uuid,
                            observacao=f"Caixa {serial}")
    _mudar_caixa(conn, serial, de_situacoes=("EM_ESTOQUE", "ABERTA"), para=caixa["situacao"],
                 posicao=destino, evento_uuid=evento_uuid, movimento_uuid=uuid, operador=operador,
                 observacao=f"Moveu de {origem} para {destino}")
    return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": False, "movimento_uuid": uuid}


def cancelar_caixa(conn, serial: str, motivo: str, *, operador: dict | None = None) -> dict:
    """IMPRESSA -> CANCELADA sem movimento; EM_ESTOQUE/ABERTA tira o saldo por AJUSTE pra menos."""
    motivo = " ".join(str(motivo or "").split())
    if not motivo:
        raise ErroWMS("Motivo obrigatorio para cancelar a caixa.")
    _fechar_transacao(conn)
    caixa = _caixa_ou_erro(conn, serial)
    serial = caixa["serial"]
    uuid = f"cx-{serial}-cancelar"
    _exigir_movimento_da_caixa(conn, uuid, caixa, ("AJUSTE",))
    if caixa["situacao"] == "IMPRESSA":
        # Queda entre o movimento de ativar e a caixa: tira o saldo orfao antes de cancelar.
        ativ = _exigir_movimento_da_caixa(conn, f"cx-{serial}-ativar", caixa, ("AJUSTE", "ENTRADA"))
        mov_uuid = None
        if ativ:
            wms.registrar_movimento(conn, tipo="AJUSTE", produto_id=ativ["produto_id"],
                                    quantidade=ativ["quantidade"], lote=ativ["lote"], validade=ativ["validade"],
                                    origem=ativ["posicao_destino"], operador=operador, uuid=uuid,
                                    observacao=f"Cancelamento da caixa {serial}: {motivo}")
            mov_uuid = uuid
        _mudar_caixa(conn, serial, de_situacoes=("IMPRESSA",), para="CANCELADA", posicao=None,
                     evento_uuid=f"{uuid}-evento", movimento_uuid=mov_uuid, operador=operador,
                     observacao=motivo, manter_posicao=True)
        return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": False, "movimento_uuid": mov_uuid}
    if caixa["situacao"] not in ("EM_ESTOQUE", "ABERTA"):
        raise ErroWMS(f"Caixa {serial} esta {caixa['situacao']} e nao pode ser cancelada.")
    mov = wms.registrar_movimento(conn, tipo="AJUSTE", produto_id=caixa["produto_id"],
                                  quantidade=caixa["qtd_un"], lote=caixa["lote"], validade=caixa["validade"],
                                  origem=caixa["posicao"], operador=operador, uuid=uuid,
                                  observacao=f"Cancelamento da caixa {serial}: {motivo}")
    _mudar_caixa(conn, serial, de_situacoes=("EM_ESTOQUE", "ABERTA"), para="CANCELADA", posicao=caixa["posicao"],
                 evento_uuid=f"{uuid}-evento", movimento_uuid=uuid, operador=operador,
                 observacao=motivo, manter_posicao=True)
    return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": bool(mov.get("duplicado")),
            "movimento_uuid": uuid}


# ---------------------------------------------------------------------------
# Baixa da expedicao, checagem caixas x saldo e zerar saldo antigo
# ---------------------------------------------------------------------------

def _log_corrida(serial: str, movimento_uuid: str) -> None:
    logging.getLogger(__name__).warning(
        "caixa %s mudou durante o consumo (%s); pulada, a checagem mostra", serial, movimento_uuid)


def consumir_caixas(conn, *, posicao: str, produto_id: int, lote: str, validade: str | None,
                    qtd_un: float, movimento_uuid: str) -> dict:
    """
    Consome `qtd_un` das caixas (posicao, produto, lote, validade) da baixa da
    expedicao: ABERTA primeiro, depois EM_ESTOQUE por serial crescente. Caixa
    consumida inteira vira EXPEDIDA (qtd_un fica como o que saiu); a parcial fica
    ABERTA com qtd_un reduzido (cancelar_caixa tira exatamente o que sobrou).
    Falta de caixa nao e erro: volta em faltou_un. NAO faz commit (roda na
    transacao de quem chama). No-op se ja houver evento com esse movimento_uuid.
    """
    movimento_uuid = str(movimento_uuid or "")
    vazio = {"expedidas": [], "aberta": None, "faltou_un": 0.0, "ja_consumido": False}
    if not movimento_uuid:
        raise ErroWMS("movimento_uuid obrigatorio para consumir caixas.")
    if conn.execute("SELECT 1 FROM wms_caixa_eventos WHERE movimento_uuid = ? LIMIT 1",
                    (movimento_uuid,)).fetchone():
        return {**vazio, "ja_consumido": True}
    falta = round(float(qtd_un), 3)
    posicao = wms.normalizar_codigo(posicao)
    lote = " ".join(str(lote or "").split()).upper()
    validade = str(validade or "")
    caixas = conn.execute(
        "SELECT serial, qtd_un, situacao FROM wms_caixas "
        "WHERE posicao = ? AND produto_id = ? AND lote = ? AND COALESCE(validade, '') = ? "
        "AND situacao IN ('ABERTA', 'EM_ESTOQUE') "
        "ORDER BY CASE situacao WHEN 'ABERTA' THEN 0 ELSE 1 END, serial",
        (posicao, produto_id, lote, validade)).fetchall()
    ts = wms.agora()
    expedidas, aberta = [], None
    for c in caixas:
        if falta <= 0.0004:
            break
        q = round(float(c["qtd_un"]), 3)
        if q <= falta + 0.0004:
            cur = conn.execute("UPDATE wms_caixas SET situacao = 'EXPEDIDA', atualizado_em = ? "
                               "WHERE serial = ? AND situacao = ? AND qtd_un = ?",
                               (ts, c["serial"], c["situacao"], c["qtd_un"]))
            if cur.rowcount != 1:
                _log_corrida(c["serial"], movimento_uuid)
                continue
            para, obs = "EXPEDIDA", f"Baixa da expedicao: {q:g} UN"
            expedidas.append(c["serial"])
            falta = round(falta - q, 3)
        else:
            cur = conn.execute("UPDATE wms_caixas SET situacao = 'ABERTA', qtd_un = ?, atualizado_em = ? "
                               "WHERE serial = ? AND situacao = ? AND qtd_un = ?",
                               (round(q - falta, 3), ts, c["serial"], c["situacao"], c["qtd_un"]))
            if cur.rowcount != 1:
                _log_corrida(c["serial"], movimento_uuid)
                continue
            para, obs = "ABERTA", f"Baixa parcial da expedicao: {falta:g} UN de {q:g}"
            aberta = c["serial"]
            falta = 0.0
        conn.execute(
            "INSERT OR IGNORE INTO wms_caixa_eventos (uuid, serial, de, para, posicao, movimento_uuid, "
            "observacao, criado_em) VALUES (?,?,?,?,?,?,?,?)",
            (f"{movimento_uuid}-{c['serial']}", c["serial"], c["situacao"], para, posicao,
             movimento_uuid, obs, ts))
    return {"expedidas": expedidas, "aberta": aberta, "faltou_un": max(falta, 0.0), "ja_consumido": False}


def divergencias(conn, embarcador_id: int) -> list[dict]:
    """Compara wms_saldos com a soma das caixas EM_ESTOQUE/ABERTA, por
    (posicao, produto, lote, validade), so de produtos do embarcador. Tolerancia
    0,001; aparecem os dois lados (saldo sem caixa e caixa sem saldo)."""
    chaves: dict[tuple, dict] = {}
    for r in conn.execute(
            "SELECT s.posicao, s.produto_id, p.sku, s.lote, s.validade, s.quantidade "
            "FROM wms_saldos s JOIN wms_produtos p ON p.id = s.produto_id "
            "WHERE p.embarcador_id = ?", (embarcador_id,)):
        k = (r["posicao"], r["produto_id"], r["lote"] or "", r["validade"] or "")
        d = chaves.setdefault(k, {"sku": r["sku"], "saldo": 0.0, "caixas": 0.0})
        d["saldo"] += float(r["quantidade"])
    for r in conn.execute(
            "SELECT c.posicao, c.produto_id, p.sku, c.lote, c.validade, SUM(c.qtd_un) AS q "
            "FROM wms_caixas c JOIN wms_produtos p ON p.id = c.produto_id "
            "WHERE p.embarcador_id = ? AND c.situacao IN ('EM_ESTOQUE', 'ABERTA') "
            "GROUP BY c.posicao, c.produto_id, c.lote, c.validade", (embarcador_id,)):
        k = (r["posicao"], r["produto_id"], r["lote"] or "", r["validade"] or "")
        d = chaves.setdefault(k, {"sku": r["sku"], "saldo": 0.0, "caixas": 0.0})
        d["caixas"] += float(r["q"] or 0)
    saida = []
    for (posicao, produto_id, lote, validade), d in sorted(chaves.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        if abs(d["saldo"] - d["caixas"]) > 0.001:
            saida.append({"posicao": posicao, "produto_id": produto_id, "sku": d["sku"], "lote": lote,
                          "validade": validade, "saldo": round(d["saldo"], 3), "caixas": round(d["caixas"], 3)})
    return saida


def plano_zerar_saldo(conn, embarcador_id: int) -> list[dict]:
    """Linhas de wms_saldos do embarcador que zerar_saldo ajustaria. Nao escreve."""
    rows = conn.execute(
        "SELECT s.posicao, s.produto_id, p.sku, s.lote, s.validade, s.quantidade "
        "FROM wms_saldos s JOIN wms_produtos p ON p.id = s.produto_id "
        "WHERE p.embarcador_id = ? AND ABS(s.quantidade) > 0.0004 "
        "ORDER BY s.posicao, s.produto_id, s.lote, s.validade", (embarcador_id,)).fetchall()
    return [dict(r) for r in rows]


def zerar_saldo(conn, embarcador_id: int, motivo: str, operador: dict | None = None) -> int:
    """Zera o saldo antigo do embarcador: um AJUSTE por linha (pra menos se > 0,
    pra mais se < 0). uuid deterministico por dia: repetir no mesmo dia nao soma
    duas vezes. Devolve quantas linhas ajustou."""
    motivo = " ".join(str(motivo or "").split())
    if not motivo:
        raise ErroWMS("Motivo obrigatorio para zerar o saldo.")
    _fechar_transacao(conn)
    if conn.execute(
            "SELECT 1 FROM wms_caixas c JOIN wms_produtos p ON p.id = c.produto_id "
            "WHERE p.embarcador_id = ? AND c.situacao IN ('EM_ESTOQUE', 'ABERTA') LIMIT 1",
            (embarcador_id,)).fetchone():
        raise ErroWMS("Ja existem caixas ativas deste embarcador; zere antes do inventario.")
    dia = wms.agora()[:10].replace("-", "")
    n = 0
    for l in plano_zerar_saldo(conn, embarcador_id):
        uuid = f"zerar-{embarcador_id}-{l['posicao']}-{l['produto_id']}-{l['lote']}-{l['validade']}-{dia}"
        q = float(l["quantidade"])
        lado = {"origem": l["posicao"]} if q > 0 else {"destino": l["posicao"]}
        wms.registrar_movimento(conn, tipo="AJUSTE", produto_id=l["produto_id"], quantidade=abs(q),
                                lote=l["lote"], validade=l["validade"] or None, operador=operador, uuid=uuid,
                                observacao=f"Zerar saldo antigo: {motivo}", **lado)
        n += 1
    return n


def main(argv=None) -> int:
    """CLI: divergencias e zerar-saldo (sem --aplicar so mostra o plano)."""
    import argparse
    ap = argparse.ArgumentParser(description="Caixas x saldo do WMS.")
    ap.add_argument("--db", help="caminho do banco (padrao: dados/dados.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_d = sub.add_parser("divergencias", help="lista onde o saldo difere da soma das caixas")
    p_d.add_argument("--embarcador-id", type=int, required=True)
    p_z = sub.add_parser("zerar-saldo", help="zera o saldo antigo do embarcador (antes do inventario por caixa)")
    p_z.add_argument("--embarcador-id", type=int, required=True)
    p_z.add_argument("--motivo", required=True)
    p_z.add_argument("--aplicar", action="store_true", help="sem isso so mostra o plano")
    args = ap.parse_args(argv)

    conn = conectar(Path(args.db) if args.db else None)
    try:
        if args.cmd == "divergencias":
            linhas = divergencias(conn, args.embarcador_id)
            for d in linhas:
                print(f"{d['posicao']}	{d['sku']}	{d['lote']}	{d['validade']}	saldo={d['saldo']}	caixas={d['caixas']}")
            print(f"{len(linhas)} divergencia(s).")
            return 0
        plano = plano_zerar_saldo(conn, args.embarcador_id)
        for l in plano:
            print(f"{l['posicao']}	{l['sku']}	{l['lote']}	{l['validade']}	saldo={l['quantidade']}")
        if not args.aplicar:
            print(f"Plano: {len(plano)} linha(s). Nada foi alterado (use --aplicar).")
            return 0
        try:
            n = zerar_saldo(conn, args.embarcador_id, args.motivo)
        except ErroWMS as e:
            print(f"Erro: {e}")
            return 1
        print(f"{n} linha(s) ajustada(s).")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.exit(main())
