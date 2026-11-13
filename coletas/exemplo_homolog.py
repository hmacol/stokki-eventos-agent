# -*- coding: utf-8 -*-
"""
coletas/exemplo_homolog.py

Lista de exemplo da tela /expedicao/coletas SO na homologacao (Hugo, 12/11):
a bancada nunca fala com a Stokki, entao coletas/ler_stokki.py nao roda la e
a tela abriria vazia. Aqui a fila e montada uma vez com a copia dos dados:
pedidos em "Aguardando Transportador" do espelho do galpao (galpao_pedidos),
com a transportadora que o batimento / os pedidos parados guardaram.

Fora da homologacao nao faz nada (preencher devolve 0).
"""
import logging
from datetime import datetime

from coletas import banco
from coletas.ler_stokki import _catalogo_padrao, eh_retirada
from homologacao.ambiente import homolog_ativo

logger = logging.getLogger("coletas.exemplo_homolog")
MAX_EXEMPLO = 80


def _transportadoras(conn) -> dict:
    """{id_stokki: transportadora} do que a copia dos dados tem."""
    tabelas = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('batimento_pedidos', 'pedidos_parados_stokki')")}
    achadas = {}
    for tabela in ("pedidos_parados_stokki", "batimento_pedidos"):   # o batimento (mais novo) vence
        if tabela in tabelas:
            for r in conn.execute(f"SELECT id_stokki, transportadora FROM {tabela} "
                                  "WHERE id_stokki IS NOT NULL AND COALESCE(transportadora, '') <> ''"):
                achadas[r[0]] = r[1]
    return achadas


def preencher(conn, catalogo=None, agora=None, forcar: bool = False) -> int:
    """Monta a fila de exemplo se ela nunca foi lida. Devolve quantos entraram."""
    if not (forcar or homolog_ativo()):
        return 0
    if conn.execute("SELECT 1 FROM expedicao_coletas_leituras LIMIT 1").fetchone():
        return 0
    quando = (agora or datetime.now()).strftime(banco.FMT)
    tem_galpao = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'galpao_pedidos'").fetchone()
    n = 0
    if tem_galpao:
        catalogo = catalogo if catalogo is not None else _catalogo_padrao()
        transportadoras = _transportadoras(conn)
        for r in conn.execute("SELECT id_stokki, codigo, embarcador FROM galpao_pedidos "
                              "WHERE situacao = 'Aguardando Transportador' AND codigo IS NOT NULL "
                              "ORDER BY id_stokki DESC").fetchall():
            nome = transportadoras.get(r["id_stokki"])
            if not nome:
                continue
            conn.execute("INSERT OR IGNORE INTO expedicao_coletas_fila (id_stokki, codigo, embarcador, transportadora,"
                         " retirada, carga_inicial, visto_primeiro_em, visto_ultimo_em) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                         (r["id_stokki"], r["codigo"], r["embarcador"], nome, int(eh_retirada(nome, catalogo)),
                          quando, quando))
            n += 1
            if n >= MAX_EXEMPLO:
                break
    conn.execute("INSERT OR REPLACE INTO expedicao_coletas_leituras (quando, resultado, obs) VALUES (?, 'ok', ?)",
                 (quando, f"exemplo da homologacao: {n} pedidos"))
    conn.commit()
    logger.info("fila de exemplo da homologacao: %s pedidos", n)
    return n
