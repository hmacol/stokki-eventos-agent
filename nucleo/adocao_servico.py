# -*- coding: utf-8 -*-
"""
nucleo/adocao_servico.py

Troca de identidade do pedido: o pedido sem servico na Vuupt vive no pool e
nos rascunhos com id sintetico negativo (-id_pool). Quando ele GANHA um
vuupt_service_id (materializacao no envio, sync da Vuupt, fila do freshhub),
o id negativo guardado nos rascunhos tem que virar o real, na mesma
transacao que grava o id no nucleo -- senao o pedido aparece duplicado.

SQL puro: o nucleo nao importa painel_agentes nem vuupt_client.
"""
import logging
import sqlite3

from nucleo import banco, normalizacao

logger = logging.getLogger(__name__)


def _tabela_existe(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone() is not None


def _trocar_na_tabela(conn: sqlite3.Connection, tabela: str, antigo: int, novo: int) -> int:
    """Reescreve service_id antigo -> novo; onde o mesmo rascunho ja tem o
    novo, apaga a linha antiga (evita estourar o UNIQUE e duplicar)."""
    if not _tabela_existe(conn, tabela):
        return 0
    n = conn.execute(f"""DELETE FROM {tabela} WHERE service_id = ?
                         AND rascunho_id IN (SELECT rascunho_id FROM {tabela} WHERE service_id = ?)""",
                     (antigo, novo)).rowcount
    n += conn.execute(f"UPDATE {tabela} SET service_id = ? WHERE service_id = ?", (novo, antigo)).rowcount
    return n


def adotar_servico(conn: sqlite3.Connection, codigo: str, service_id: int) -> int:
    """Grava `vuupt_service_id` no nucleo e troca -id_pool -> service_id em
    rascunhos_parada e rascunhos_plano_original. Devolve quantas linhas de
    rascunho foram reescritas (ou apagadas por duplicidade).

    Nao faz commit: participa da transacao do chamador, que decide o commit.
    Idempotente. Se o codigo ja tem OUTRO id real, nao troca nada (WARNING,
    devolve 0). Tabelas de rascunho ausentes sao ignoradas."""
    codigo = normalizacao.normalizar_codigo(codigo)
    if not codigo or not service_id:
        return 0
    service_id = int(service_id)
    linha = conn.execute("SELECT id_pool, vuupt_service_id FROM nucleo_pedidos WHERE codigo = ?", (codigo,)).fetchone()
    if linha is None:
        return 0
    id_pool, atual = linha[0], linha[1]
    if atual is not None and int(atual) != service_id:
        logger.warning(f"adotar_servico {codigo}: ja tem o servico {atual}, nao troca por {service_id}")
        return 0
    if atual is None:
        conn.execute("UPDATE nucleo_pedidos SET vuupt_service_id = ?, atualizado_em = ? WHERE codigo = ?",
                     (service_id, banco.agora(), codigo))
    if id_pool is None:
        return 0
    antigo = -int(id_pool)
    return (_trocar_na_tabela(conn, "rascunhos_parada", antigo, service_id)
            + _trocar_na_tabela(conn, "rascunhos_plano_original", antigo, service_id))
