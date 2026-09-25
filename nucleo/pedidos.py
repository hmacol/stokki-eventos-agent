# -*- coding: utf-8 -*-
"""
nucleo/pedidos.py

Espelho local dos pedidos (nucleo_pedidos) -- Fase A. Duas fontes:

  1. pipeline.py (dual-write): logo depois de criar_ou_atualizar_servico
     na VUUPT, registrar_importacao(payload, resposta, acao) grava o mesmo
     pedido aqui. Best-effort -- o pipeline chama dentro de try/except.
  2. nucleo/sincronizar_vuupt.py: serviços que aparecem dentro das rotas
     sincronizadas (status/completed_at) atualizam o espelho também.

Chave: `codigo` (PS-XXXXX), o mesmo elo Stokki <-> VUUPT usado em todo o
projeto. Sempre upsert; nunca apaga.
"""
import json
import logging
import sqlite3

from nucleo import banco, normalizacao

logger = logging.getLogger(__name__)


def _servico_da_resposta(resposta) -> dict:
    """POST/PUT /services devolve {"service": {...}} ou o objeto direto
    (mesmo tratamento de vuupt_client.criar_ou_atualizar_servico)."""
    if not isinstance(resposta, dict):
        return {}
    servico = resposta.get("service", resposta)
    return servico if isinstance(servico, dict) else {}


def _campos_do_payload(payload: dict) -> dict:
    customer = payload.get("customer") or {}
    sender = payload.get("sender") or {}
    return {
        "titulo": payload.get("title"),
        "tipo": payload.get("type") or "delivery",
        "destinatario_nome": customer.get("name"),
        "destinatario_codigo": customer.get("code"),
        "destinatario_telefone": customer.get("phone_number") or payload.get("phone_number"),
        "endereco": payload.get("address") or customer.get("address"),
        "latitude": payload.get("latitude", customer.get("latitude")),
        "longitude": payload.get("longitude", customer.get("longitude")),
        "horario_inicio": customer.get("operating_hour_start"),
        "horario_fim": customer.get("operating_hour_end"),
        "remetente_nome": sender.get("name"),
        "remetente_codigo": sender.get("code"),
        "sender_id": payload.get("sender_id"),
        "caixas": payload.get("dimension_3"),
        # O pipeline monta o agendamento em hora de Brasília (com offset
        # -03:00, ver vuupt_client._converter_data_para_iso); no núcleo
        # tudo é hora local sem fuso.
        "agendamento_inicio": normalizacao.para_local(payload.get("scheduled_start")),
        "agendamento_fim": normalizacao.para_local(payload.get("scheduled_end")),
    }


def upsert_pedido(conn: sqlite3.Connection, codigo: str, campos: dict, origem: str,
                  dados_json: dict | None = None, status: str | None = None,
                  vuupt_service_id: int | None = None):
    """Upsert pelo código NORMALIZADO (sem '#', maiúsculo). Campos None não
    sobrescrevem valor já gravado -- uma fonte parcial (ex.: serviço dentro
    da rota, sem telefone) nunca apaga o que o pipeline gravou.

    Corrigido em 15/09 (Etapa 2 do DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md),
    depois de achar 15 pedidos entregues marcados ABERTO em produção:

    - `status=None` significa "não sei, mantenha o que está lá", e é o que
      o sync manda em rota cancelada e o pipeline manda em pedido que ele
      só reimportou. O `excluded.status` recebia 'ABERTO' e o COALESCE
      nunca via NULL, então TODA rodada do pipeline (18h e 22h) devolvia
      pra ABERTO pedido que já estava entregue ou em rota. Agora o valor
      cru vai separado no UPDATE e o DEFAULT só vale no INSERT.
    - `origem` é quem CRIOU a linha; o último a tocar não reescreve mais.
    - `dados_json` é mesclado (json_patch), não substituído: o payload do
      pipeline convivia com o serviço da VUUPT e um apagava o outro.
    """
    codigo = normalizacao.normalizar_codigo(codigo)
    if not codigo:
        return
    # `tipo` e `status` andam juntos: têm DEFAULT no INSERT e, no UPDATE,
    # None quer dizer "mantenha" (por isso vão como parâmetro cru, fora do
    # excluded.*, que já viria com o DEFAULT aplicado).
    colunas = [
        "titulo", "destinatario_nome", "destinatario_codigo", "destinatario_telefone",
        "endereco", "latitude", "longitude", "horario_inicio", "horario_fim",
        "remetente_nome", "remetente_codigo", "sender_id", "caixas",
        "agendamento_inicio", "agendamento_fim",
        # 16/09: o que o espelho de SERVIÇOS (fora de rota) traz --
        # quem não souber o valor manda None e o que está gravado fica.
        "status_provedor", "status_done_provedor", "customer_id", "vuupt_route_id", "driver_id",
        "nota", "complemento", "fluxo", "criado_em_provedor", "atualizado_em_provedor",
        "excluido_em", "reentrega_de_service_id", "reentrega_de_codigo", "zona", "zona_id",
        "qtd_checklists", "qtd_anexos",
    ]
    valores = [campos.get(c) for c in colunas]
    tipo = campos.get("tipo")
    json_txt = json.dumps(dados_json, ensure_ascii=False, default=str) if dados_json is not None else None

    set_coalesce = ", ".join(f"{c} = COALESCE(excluded.{c}, nucleo_pedidos.{c})" for c in colunas)
    conn.execute(f"""
        INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, {", ".join(colunas)}, tipo, status,
                                    origem, dados_json, atualizado_em)
        VALUES (?, ?, {", ".join("?" for _ in colunas)}, COALESCE(?, 'delivery'), COALESCE(?, ?), ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET
            vuupt_service_id = COALESCE(excluded.vuupt_service_id, nucleo_pedidos.vuupt_service_id),
            {set_coalesce},
            tipo = COALESCE(?, nucleo_pedidos.tipo),
            status = COALESCE(?, nucleo_pedidos.status),
            origem = COALESCE(nucleo_pedidos.origem, excluded.origem),
            dados_json = CASE
                WHEN excluded.dados_json IS NULL THEN nucleo_pedidos.dados_json
                WHEN json_valid(nucleo_pedidos.dados_json) AND json_valid(excluded.dados_json)
                    THEN json_patch(nucleo_pedidos.dados_json, excluded.dados_json)
                ELSE excluded.dados_json END,
            atualizado_em = excluded.atualizado_em
    """, (codigo, vuupt_service_id, *valores, tipo, status, banco.PEDIDO_ABERTO, origem, json_txt,
          banco.agora(), tipo, status))


def marcar_em_rota_por_service_ids(service_ids, vuupt_route_id: int, conn: sqlite3.Connection | None = None) -> int:
    """Gancho do envio do rascunho (rascunhos_rota.enviar_rascunho, Hugo
    24/09): a rota acabou de ser criada na Vuupt e o route_id já é conhecido,
    então o pedido vira EM_ROTA aqui na hora, sem ir buscar o serviço. Só
    mexe em quem estava ABERTO (entregue/cancelado não volta). Devolve
    quantos mudaram."""
    ids = [int(i) for i in (service_ids or []) if i]
    if not ids:
        return 0
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        marcadores = ",".join("?" * len(ids))
        cur = conn.execute(f"""UPDATE nucleo_pedidos SET status = ?, status_provedor = 'assigned', vuupt_route_id = ?,
                                      atualizado_em = ?
                               WHERE vuupt_service_id IN ({marcadores}) AND status = ?""",
                           (banco.PEDIDO_EM_ROTA, vuupt_route_id, banco.agora(), *ids, banco.PEDIDO_ABERTO))
        conn.commit()
        return cur.rowcount
    finally:
        if fechar:
            conn.close()


def registrar_importacao(payload: dict, resposta, acao: str, conn: sqlite3.Connection | None = None):
    """
    Gancho do pipeline.py (dual-write). `acao` é o retorno de
    criar_ou_atualizar_servico ('criado' | 'atualizado' |
    'pulado_atribuido' | 'pulado_sem_alteracao'). Nos casos "pulado" a
    VUUPT não foi tocada, mas o espelho ganha o pedido mesmo assim (pode
    ser a primeira vez que ele passa por aqui) -- sem service_id.
    """
    codigo = (payload or {}).get("code") or ""
    if not codigo:
        return
    servico = _servico_da_resposta(resposta)
    service_id = servico.get("id")

    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        upsert_pedido(
            conn, codigo, _campos_do_payload(payload), origem=banco.ORIGEM_PIPELINE,
            dados_json={"payload": payload, "acao": acao, "service": servico or None},
            vuupt_service_id=int(service_id) if service_id is not None else None,
        )
        conn.commit()
    finally:
        if fechar:
            conn.close()


def buscar_pedido(codigo: str, conn: sqlite3.Connection | None = None) -> dict | None:
    """Aceita o código em qualquer forma ('#PS-1', 'ps-1'): a chave é a
    normalizada."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        row = conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo = ?",
                           (normalizacao.normalizar_codigo(codigo),)).fetchone()
        return dict(row) if row else None
    finally:
        if fechar:
            conn.close()