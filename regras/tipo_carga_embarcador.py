# -*- coding: utf-8 -*-
"""
regras/tipo_carga_embarcador.py

Tipo de carga (Seco/Refrigerado/Congelado) por SENDER_ID do VUUPT --
usado em criar_rotas_diarias.py (pedido do Hugo, 10/08: "as entregas
Secas deveriam ser roteirizadas separadas das refrigeradas e
congeladas") pra decidir em qual partição (Seco x Refrigerado/Congelado)
cada pedido not_assigned entra ANTES do agrupamento geográfico.

Mesma fonte que pipeline.py já usa pra montar a skill na criação do
serviço (tabela 'interno', coluna habilidade) -- aqui é só a mesma
informação, indexada pelo lado inverso: sender_id (que VOLTA no próprio
serviço do VUUPT, ver GET /services) -> tipo de carga, em vez de
stkkc_id (que só existe do lado da Stokki, não está mais disponível
depois que o pedido já virou serviço no VUUPT).
"""
import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

TIPOS_CARGA_VALIDOS = {"Congelado", "Seco", "Refrigerado"}
TIPO_CARGA_PADRAO = "Seco"

# Tipos de carga que devem ser roteirizados JUNTOS, separados de "Seco"
# (pedido do Hugo, 10/08).
TIPOS_CARGA_FRIA = {"Refrigerado", "Congelado"}


def carregar_tipos_carga_por_sender(db_path: str | Path) -> dict[int, str]:
    """
    Lê a tabela 'interno' e retorna {sender_id: tipo_carga}, já validado
    contra TIPOS_CARGA_VALIDOS (entrada inválida/vazia é ignorada --
    quem consulta cai no padrão via classificar_tipo_carga).

    Banco ausente ou sem linhas: retorna {} (todo sender_id cai no
    padrão) -- não é erro fatal, só um aviso no log.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        logger.warning(
            f"Banco '{db_path}' não encontrado -- todos os pedidos usarão "
            f"o tipo de carga padrão ({TIPO_CARGA_PADRAO})."
        )
        return {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sender_id, habilidade FROM interno WHERE sender_id IS NOT NULL"
    ).fetchall()
    conn.close()

    mapa: dict[int, str] = {}
    for row in rows:
        habilidade = (row["habilidade"] or "").strip().capitalize()
        if habilidade in TIPOS_CARGA_VALIDOS:
            mapa[row["sender_id"]] = habilidade
    logger.info(f"Tipo de carga por sender_id carregado: {len(mapa)} embarcador(es) com habilidade válida.")
    return mapa


def classificar_tipo_carga(sender_id, mapa: dict[int, str]) -> tuple[str, bool]:
    """
    Retorna (tipo_carga, encontrado_no_mapa).

    Se sender_id vier vazio/None ou não estiver no mapa, aplica
    TIPO_CARGA_PADRAO e retorna encontrado_no_mapa=False.
    """
    if sender_id in mapa:
        return mapa[sender_id], True
    return TIPO_CARGA_PADRAO, False
