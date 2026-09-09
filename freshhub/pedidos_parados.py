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
import re

from freshhub.auth import SUPABASE_URL, FreshHubSession

logger = logging.getLogger(__name__)

# Quem registra o pedido parado no Fresh Hub às vezes digita o código
# inteiro ("PS-36327", "PS.36327", "ps 36327", "#PS-36327") em vez de só
# o número (pedido do Hugo, 09/09). Tudo que consome esse valor -- link
# da Stokki (/outbound/show/{id}), busca por code "PS-{n}" na Vuupt,
# filtro de entregues, chaves das tabelas locais de classificação --
# espera SÓ os dígitos, então o prefixo é tirado aqui, na fonte.
_PREFIXO_PS = re.compile(r"^\s*#?\s*PS\s*[.\-_ ]*\s*(\d+)\s*$", re.IGNORECASE)


def normalizar_order_number(valor) -> str:
    """'PS-36327' / 'PS.36327' / '#PS 36327' -> '36327'; o resto sai
    como veio (só sem espaços nas pontas), porque pode ser a NF do
    cliente e aí não há o que limpar."""
    texto = str(valor or "").strip()
    m = _PREFIXO_PS.match(texto)
    return m.group(1) if m else texto


def listar_pedidos_parados(sessao: FreshHubSession, limit: int = 200) -> list[dict]:
    """
    Retorna os pedidos parados, mais recentes primeiro.

    Cada item (campos crus da tabela stalled_orders, com `order_number`
    já normalizado por normalizar_order_number -- o valor original fica
    em `order_number_bruto`):
      {
        "id":             str (uuid),
        "order_number":   str,   -- número do pedido; pode ser o ID do
                                     Stokki (parte numeral de PS-XXXXX)
                                     OU a NF do cliente -- varia por
                                     registro, ver doc.
        "order_number_bruto": str,  -- como foi digitado no Fresh Hub
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
    for p in pedidos:
        bruto = p.get("order_number")
        p["order_number_bruto"] = bruto
        p["order_number"] = normalizar_order_number(bruto)
    if not pedidos:
        logger.warning(
            "0 pedidos parados retornados -- se você esperava resultados, "
            "confirme se a sessão está realmente autenticada: o RLS do "
            "Supabase bloqueia sem token válido devolvendo 200 com lista "
            "vazia, não um erro (confirmado em 24/08)."
        )
    return pedidos
