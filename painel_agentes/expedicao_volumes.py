# -*- coding: utf-8 -*-
"""
painel_agentes/expedicao_volumes.py

Etiqueta de volume pra TODO pedido finalizado na Stokki (28/11/2026), cross incluido.
O operador digita ou bipa o PS, informa quantas caixas tem e as etiquetas 10x15 saem
pela fila de impressao (wms_fila_impressao, tipo VOLUME). Cada caixa tem um id unico
no QR (`FV|PS|n|total|id`): e ele que a bipagem do carregamento vai conferir depois.

Modulo de dados puro (sem Flask). Nunca chama a Stokki nem a Vuupt: le so o nosso
banco, e tabela de outro modulo que nao existir conta como fonte vazia.
O piloto do galpao (wms_volumes, QR de 4 campos) continua em wms_separacao.py.

Spec: docs/superpowers/specs/2026-11-28-etiqueta-volume-pedidos-stokki-design.md
"""
import re
import sqlite3

import wms

_DDL = (
    """
CREATE TABLE IF NOT EXISTS expedicao_volumes (
    id_caixa     TEXT PRIMARY KEY,          -- o <id> do QR
    codigo       TEXT NOT NULL,             -- PS-<n>, pedido base
    numero       INTEGER NOT NULL,
    total        INTEGER NOT NULL,
    impressao_id INTEGER,
    reimpressoes INTEGER NOT NULL DEFAULT 0,
    criado_em    TEXT NOT NULL,
    criado_por   TEXT NOT NULL DEFAULT '',
    anulado_em   TEXT,
    anulado_por  TEXT
)
""",
    """
CREATE INDEX IF NOT EXISTS idx_expedicao_volumes_codigo
    ON expedicao_volumes (codigo, anulado_em)
""",
)

STATUS_PRONTO_STOKKI = "Waiting for Carrier"   # "aguardando transportador" no retrato do pipeline

_RE_CODIGO = re.compile(r"^#?\s*(?:PS-?)?(\d+)(?:-R\d+)?$")


def garantir_tabela(conn) -> None:
    """Cria a tabela se nao existir (idempotente)."""
    for stmt in _DDL:
        conn.execute(stmt)


def codigo_base(texto) -> str:
    """'#ps-45352' / '45352' / 'PS-45352-R1' / o QR de uma etiqueta -> 'PS-45352'.
    Reentrega (-R) e o mesmo pedido na Stokki: a caixa e a mesma."""
    bruto = str(texto if texto is not None else "").strip().upper()
    if bruto.startswith("FV|"):
        partes = bruto.split("|")
        bruto = partes[1].strip() if len(partes) > 1 else ""
    m = _RE_CODIGO.match(bruto)
    if not m:
        raise wms.ErroWMS(f"Numero de pedido invalido: {bruto or 'vazio'}. Use o numero da Stokki (ex.: PS-45352).")
    return f"PS-{int(m.group(1))}"


def _ler(conn, sql: str, params: tuple):
    """Primeira linha como dict, ou None. Tabela/coluna de outro modulo que nao existe = None."""
    try:
        cur = conn.execute(sql, params)
        row = cur.fetchone()
    except sqlite3.OperationalError as e:
        if "no such table" in str(e).lower() or "no such column" in str(e).lower():
            return None
        raise
    if row is None:
        return None
    return dict(zip([c[0] for c in cur.description], row))


def dados_do_pedido(conn, codigo: str) -> dict:
    """O que vai na etiqueta e na tela, pelo codigo base. Ordem das fontes: nucleo_pedidos,
    wms_pedidos, vigia_stokki_abertos; cada uma so preenche o que ainda esta vazio."""
    d = {"codigo": codigo, "conhecido": False, "embarcador": "", "destinatario": "", "endereco": "",
         "cidade": "", "uf": "", "nf": "", "cancelado": False, "sugestao": None}

    def completar(campos: dict):
        d["conhecido"] = True
        for chave, valor in campos.items():
            if not d[chave] and valor:
                d[chave] = str(valor).strip()

    nucleo = _ler(conn, "SELECT destinatario_nome, endereco, remetente_nome, status FROM nucleo_pedidos "
                        "WHERE codigo = ?", (codigo,))
    if nucleo:
        completar({"destinatario": nucleo["destinatario_nome"], "endereco": nucleo["endereco"],
                   "embarcador": nucleo["remetente_nome"]})
        d["cancelado"] = str(nucleo["status"] or "").upper() == "CANCELADO"

    galpao = _ler(conn, "SELECT * FROM wms_pedidos WHERE upper(replace(codigo_ps, '#', '')) = ? "
                        "ORDER BY id DESC LIMIT 1", (codigo,))
    if galpao:
        completar({"embarcador": galpao.get("embarcador"), "destinatario": galpao.get("destinatario"),
                   "endereco": galpao.get("endereco_destino"), "cidade": galpao.get("cidade"),
                   "uf": galpao.get("uf"), "nf": galpao.get("nf")})

    retrato = _ler(conn, "SELECT embarcador, status_stokki FROM vigia_stokki_abertos WHERE codigo = ?", (codigo,))
    if retrato:
        completar({"embarcador": retrato["embarcador"]})
        # Hugo, 28/11: so sugere o total CONFERIDO, e so com a Stokki em aguardando transportador
        if retrato["status_stokki"] == STATUS_PRONTO_STOKKI:
            lido = _ler(conn, "SELECT volumes, fonte FROM volumes_conferidos WHERE id_pedido = ?",
                        (int(codigo.split("-")[1]),))
            if lido and lido["fonte"] == "conferencia" and lido["volumes"]:
                d["sugestao"] = int(lido["volumes"])
    return d
