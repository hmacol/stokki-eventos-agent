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

from nucleo import banco

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
        "agendamento_inicio": payload.get("scheduled_start"),
        "agendamento_fim": payload.get("scheduled_end"),
    }


def upsert_pedido(conn: sqlite3.Connection, codigo: str, campos: dict, origem: str,
                  dados_json: dict | None = None, status: str | None = None,
                  vuupt_service_id: int | None = None):
    """Upsert por código. Campos None NÃO sobrescrevem valor já gravado
    (COALESCE) -- uma fonte parcial (ex.: serviço dentro da rota, sem
    telefone) nunca apaga o que o pipeline já tinha gravado."""
    colunas = [
        "titulo", "tipo", "destinatario_nome", "destinatario_codigo", "destinatario_telefone",
        "endereco", "latitude", "longitude", "horario_inicio", "horario_fim",
        "remetente_nome", "remetente_codigo", "sender_id", "caixas",
        "agendamento_inicio", "agendamento_fim",
    ]
    campos = {**campos, "tipo": campos.get("tipo") or "delivery"}  # NOT NULL na tabela
    valores = [campos.get(c) for c in colunas]
    json_txt = json.dumps(dados_json, ensure_ascii=False, default=str) if dados_json is not None else None

    set_coalesce = ", ".join(f"{c} = COALESCE(excluded.{c}, nucleo_pedidos.{c})" for c in colunas)
    conn.execute(f"""
        INSERT INTO nucleo_pedidos (codigo, vuupt_service_id, {", ".join(colunas)}, status, origem, dados_json, atualizado_em)
        VALUES (?, ?, {", ".join("?" for _ in colunas)}, ?, ?, ?, ?)
        ON CONFLICT(codigo) DO UPDATE SET
            vuupt_service_id = COALESCE(excluded.vuupt_service_id, nucleo_pedidos.vuupt_service_id),
            {set_coalesce},
            status = COALESCE(excluded.status, nucleo_pedidos.status),
            origem = excluded.origem,
            dados_json = COALESCE(excluded.dados_json, nucleo_pedidos.dados_json),
            atualizado_em = excluded.atualizado_em
    """, (codigo, vuupt_service_id, *valores, status or banco.PEDIDO_ABERTO, origem, json_txt, banco.agora()))


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
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        row = conn.execute("SELECT * FROM nucleo_pedidos WHERE codigo = ?", (codigo,)).fetchone()
        return dict(row) if row else None
    finally:
        if fechar:
            conn.close()