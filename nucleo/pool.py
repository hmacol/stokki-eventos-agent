# -*- coding: utf-8 -*-
"""
nucleo/pool.py

O pool do planejamento lido do NÚCLEO (Entrega 1 do spec
docs/superpowers/specs/2026-09-24-pool-pelo-nucleo-pedidos-portal-rascunho-14h-design.md,
Etapa 4 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md).

Até aqui a Vuupt era dona do pool: planejamento_rotas.buscar_pool_e_agendados,
roteirizar_selecionados, criar_rotas_diarias.py e incrementar_rotas.py liam
`not_assigned` ao vivo. Aqui cada pedido ABERTO de nucleo_pedidos volta no
MESMO formato do serviço da Vuupt (GET /services com include=customer), então
_servico_para_pool, resolver_janela, regra_dia_fixo_do_servico,
extrair_volume_caixas e chegou_dentro_do_corte continuam lendo o que sempre
leram. Horários voltam como a Vuupt devolve (UTC sem fuso, ver
normalizacao.local_para_vuupt): esta entrega NÃO muda o que as telas mostram.

A fonte é escolhida por `planejamento.fonte_pool` no config.yaml
('vuupt' | 'nucleo'; ausente = vuupt). Voltar atrás é trocar a chave.

Este módulo NÃO importa vuupt_client de propósito (nucleo/test_acoplamento_vuupt):
quem precisa da Vuupt passa a instância pronta.
"""
import sqlite3

from nucleo import banco
from nucleo.normalizacao import local_para_vuupt

FONTE_VUUPT = "vuupt"
FONTE_NUCLEO = "nucleo"

_SQL_BASE = """SELECT * FROM nucleo_pedidos
               WHERE status = ? AND COALESCE(fluxo, ?) = ? AND excluido_em IS NULL"""


def fonte_pool(config: dict | None) -> str:
    valor = ((config or {}).get("planejamento") or {}).get("fonte_pool") or FONTE_VUUPT
    return str(valor).strip().lower()


def pedido_para_servico(p) -> dict:
    """Linha de nucleo_pedidos -> dict no formato do serviço da Vuupt. As
    chaves com '_' na frente são nossas (a tela usa '_fonte' só pra depurar)."""
    return {
        "id": p["vuupt_service_id"],
        "code": f"#{p['codigo']}",
        "title": p["titulo"],
        "type": p["tipo"],
        "status": p["status_provedor"] or "not_assigned",
        "status_done": p["status_done_provedor"],
        "address": p["endereco"],
        "address_complement": p["complemento"],
        "latitude": p["latitude"],
        "longitude": p["longitude"],
        "sender_id": p["sender_id"],
        "dimension_3": p["caixas"],
        "note": p["nota"],
        "customer_id": p["customer_id"],
        "route_id": p["vuupt_route_id"],
        "driver_id": p["driver_id"],
        "scheduled_start": local_para_vuupt(p["agendamento_inicio"]),
        "scheduled_end": local_para_vuupt(p["agendamento_fim"]),
        "created_at": local_para_vuupt(p["criado_em_provedor"] or p["criado_em"]),
        "updated_at": local_para_vuupt(p["atualizado_em_provedor"] or p["atualizado_em"]),
        "deleted_at": None,
        "recreated_order_origin_id": p["reentrega_de_service_id"],
        "customer": {
            "name": p["destinatario_nome"],
            "code": p["destinatario_codigo"],
            "phone_number": p["destinatario_telefone"],
            "operating_hour_start": p["horario_inicio"],
            "operating_hour_end": p["horario_fim"],
        },
        "_fonte": FONTE_NUCLEO,
        "_origem": p["origem"],
    }


def listar_pool(conn: sqlite3.Connection | None = None) -> list[dict]:
    """O que hoje é `not_assigned` na Vuupt: pedido ABERTO, de entrega, não
    excluído, e COM serviço na Vuupt (pedido "pulado" pelo pipeline fica
    fora: sem id não vira parada de rascunho -- rascunhos_parada.service_id
    é NOT NULL até a Entrega 2). Um serviço = um item: se duas linhas
    apontam pro mesmo id (serviço que absorveu outro código), fica a
    atualizada por último."""
    sql = _SQL_BASE + " AND vuupt_service_id IS NOT NULL"
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        por_id: dict[int, sqlite3.Row] = {}
        for p in conn.execute(sql, (banco.PEDIDO_ABERTO, banco.FLUXO_ENTREGA, banco.FLUXO_ENTREGA)):
            atual = por_id.get(p["vuupt_service_id"])
            if atual is None or (p["atualizado_em"] or "") > (atual["atualizado_em"] or ""):
                por_id[p["vuupt_service_id"]] = p
    finally:
        if fechar:
            conn.close()
    return sorted((pedido_para_servico(p) for p in por_id.values()), key=lambda s: s["code"])


def listar_pool_not_assigned(config: dict | None, vuupt) -> list[dict]:
    """A troca de fonte, num lugar só. `vuupt` é um VuuptClient já criado
    por quem chama (só é usado com fonte 'vuupt'). A chamada à Vuupt é
    EXATAMENTE a que os consumidores faziam antes desta função existir."""
    if fonte_pool(config) == FONTE_NUCLEO:
        return listar_pool()
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    return vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
