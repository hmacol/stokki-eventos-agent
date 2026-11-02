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
                 observacao: str = "", manter_posicao: bool = False) -> None:
    """UPDATE da caixa + evento numa transacao curta. Idempotente pelo uuid do evento."""
    op_id, op_nome = _op(operador)
    marcas = ",".join("?" * len(de_situacoes))
    conn.execute("BEGIN IMMEDIATE")
    try:
        atual = conn.execute("SELECT situacao, posicao FROM wms_caixas WHERE serial = ?", (serial,)).fetchone()
        ts = wms.agora()
        if manter_posicao:
            conn.execute(f"UPDATE wms_caixas SET situacao = ?, atualizado_em = ? "
                         f"WHERE serial = ? AND situacao IN ({marcas})", (para, ts, serial, *de_situacoes))
        else:
            conn.execute(f"UPDATE wms_caixas SET situacao = ?, posicao = ?, atualizado_em = ? "
                         f"WHERE serial = ? AND situacao IN ({marcas})", (para, posicao, ts, serial, *de_situacoes))
        conn.execute(
            "INSERT OR IGNORE INTO wms_caixa_eventos (uuid, serial, de, para, posicao, movimento_uuid, "
            "operador_id, operador_nome, observacao, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (evento_uuid, serial, atual["situacao"] if atual else None, para,
             posicao if posicao is not None else (atual["posicao"] if atual else None),
             movimento_uuid, op_id, op_nome, observacao[:500], ts))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _contabilizar(conn, caixa: dict, recebimento_id, item_id, uuid: str) -> None:
    if recebimento_id is not None and item_id is not None:
        wms_pedidos.contabilizar_enderecamento(conn, recebimento_id, item_id, caixa["qtd_un"], uuid=uuid)


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
        # Resposta perdida: completa a barra do recebimento (idempotente pelo uuid).
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

    if caixa["situacao"] not in ("EM_ESTOQUE", "ABERTA"):
        raise ErroWMS(f"Caixa {serial} esta {caixa['situacao']} e nao pode ser movida.")
    ja = conn.execute("SELECT posicao_destino FROM wms_movimentos WHERE uuid = ?", (uuid,)).fetchone()
    if ja:
        # Repeticao depois de queda: o saldo ja andou; so garante a caixa na posicao do movimento.
        _mudar_caixa(conn, serial, de_situacoes=("EM_ESTOQUE", "ABERTA"), para=caixa["situacao"],
                     posicao=ja["posicao_destino"], evento_uuid=evento_uuid, movimento_uuid=uuid,
                     operador=operador, observacao=f"Moveu para {ja['posicao_destino']}")
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
    if caixa["situacao"] == "IMPRESSA":
        _mudar_caixa(conn, serial, de_situacoes=("IMPRESSA",), para="CANCELADA", posicao=None,
                     evento_uuid=f"{uuid}-evento", movimento_uuid=None, operador=operador,
                     observacao=motivo, manter_posicao=True)
        return {"caixa": _caixa_ou_erro(conn, serial), "duplicado": False, "movimento_uuid": None}
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
