# -*- coding: utf-8 -*-
"""
freshhub/pedidos_parados.py

Leitura da tabela `stalled_orders` -- fonte real da tela "Pedidos
Parados" do Fresh Hub (freshhub.com.br/pedidos-parados), por trás da
aba Histórico. Schema confirmado em 24/08 a partir de uma amostra real
da API (ver TRATATIVAS_PEDIDOS_PARADOS.md na raiz do projeto pro
contexto de negócio completo -- classificação, prioridade, tratativas).

Importante: não existe coluna de status/classificação nesta tabela --
o "Status: Pendente" mostrado na UI do Fresh Hub não é um dado
persistido com várias opções, é combinado com o Hugo que a
classificação (Cancelados/Devolução Parcial/Reenvio/Agendado) é NOSSA,
não do Fresh Hub.
"""
import logging

from freshhub.auth import SUPABASE_URL, FreshHubSession

logger = logging.getLogger(__name__)


def listar_pedidos_parados(sessao: FreshHubSession, limit: int = 200) -> list[dict]:
    """
    Retorna os pedidos parados, mais recentes primeiro.

    Cada item (campos crus da tabela stalled_orders):
      {
        "id":             str (uuid),
        "order_number":   str,   -- número do pedido; pode ser o ID do
                                     Stokki (parte numeral de PS-XXXXX)
                                     OU a NF do cliente -- varia por
                                     registro, ver doc.
        "volumes":        int,
        "photo_url":      str | None,
        "recebedor_name": str,   -- quem registrou como parado
        "recebedor_id":   str (uuid),
        "resolved_at":    str | None,
        "created_at":     str,   -- quando foi registrado como parado
      }
    """
    resp = sessao.get(
        f"{SUPABASE_URL}/rest/v1/stalled_orders",
        params={"select": "*", "order": "created_at.desc", "limit": limit},
    )
    resp.raise_for_status()
    pedidos = resp.json()
    if not pedidos:
        logger.warning(
            "0 pedidos parados retornados -- se você esperava resultados, "
            "confirme se a sessão está realmente autenticada: o RLS do "
            "Supabase bloqueia sem token válido devolvendo 200 com lista "
            "vazia, não um erro (confirmado em 24/08)."
        )
    return pedidos
