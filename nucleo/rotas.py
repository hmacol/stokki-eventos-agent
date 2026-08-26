# -*- coding: utf-8 -*-
"""
nucleo/rotas.py

Rotas e paradas materializadas no núcleo próprio (nucleo_rotas /
nucleo_paradas / nucleo_eventos) -- Fase A.

Entrada principal hoje: registrar_rota_enviada(rascunho_id, vuupt_route_id),
chamada (best-effort) por painel_agentes/rascunhos_rota.marcar_enviado --
toda rota que vai pra VUUPT passa a existir aqui no mesmo instante, com
provedor=VUUPT, snapshot das paradas na ordem enviada e km_estimado do
rascunho. É o começo do histórico que a VUUPT nunca deu
(torre_controle.py: "tudo ao vivo na VUUPT").

Na Fase C, o mesmo módulo materializa rotas com provedor=APP (sem passar
pela VUUPT) -- `materializar_rascunho(rascunho_id, provedor=APP)`.

Uma rota = uma fonte de verdade. `vuupt_route_id` é único (índice parcial);
reenviar o mesmo rascunho depois de cancelar cria OUTRA rota aqui (a
antiga fica CANCELADA), espelhando o que acontece na VUUPT.
"""
import json
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "painel_agentes"))

from nucleo import banco

logger = logging.getLogger(__name__)


def _json(valor) -> str | None:
    return json.dumps(valor, ensure_ascii=False, default=str) if valor is not None else None


def registrar_evento(conn: sqlite3.Connection, tipo: str, origem: str, ocorrido_em: str | None = None,
                     rota_id: int | None = None, parada_id: int | None = None, agent_id: int | None = None,
                     latitude: float | None = None, longitude: float | None = None,
                     precisao_m: float | None = None, dados: dict | None = None,
                     uuid: str | None = None) -> int | None:
    """Insere um evento. `uuid` (quando vem do app) garante idempotência:
    repetido é ignorado e devolve None."""
    cur = conn.execute("""
        INSERT INTO nucleo_eventos (uuid, rota_id, parada_id, agent_id, tipo, origem, ocorrido_em,
                                    latitude, longitude, precisao_m, dados_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(uuid) DO NOTHING
    """, (uuid, rota_id, parada_id, agent_id, tipo, origem, ocorrido_em or banco.agora(),
          latitude, longitude, precisao_m, _json(dados)))
    return cur.lastrowid if cur.rowcount else None


def _inserir_rota(conn: sqlite3.Connection, rascunho: dict, provedor: str,
                  vuupt_route_id: int | None) -> int:
    start_at = rascunho.get("start_at") or ""
    data_rota = rascunho.get("data_alvo") or start_at[:10]
    cur = conn.execute("""
        INSERT INTO nucleo_rotas (data_rota, nome, provedor, vuupt_route_id, rascunho_id, agent_id, vehicle_id,
                                  motorista_nome, tipo_veiculo, start_at, start_location_base_id,
                                  end_location_base_id, km_estimado, km_fonte, status, total_paradas, dados_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data_rota, rascunho.get("nome"), provedor, vuupt_route_id, rascunho.get("id"),
        rascunho.get("agent_id"), rascunho.get("vehicle_id"), rascunho.get("motorista_nome"),
        rascunho.get("tipo_veiculo"), start_at, rascunho.get("start_location_base_id"),
        rascunho.get("end_location_base_id"), rascunho.get("km_estimado"),
        "ESTIMADO" if rascunho.get("km_estimado") is not None else None,
        banco.ROTA_PLANEJADA, len(rascunho.get("paradas") or []),
        _json({"rascunho": {k: v for k, v in rascunho.items() if k != "paradas"}}),
    ))
    rota_id = cur.lastrowid
    for p in rascunho.get("paradas") or []:
        conn.execute("""
            INSERT INTO nucleo_paradas (rota_id, ordem, service_id, codigo, titulo, destinatario_nome, endereco,
                                        latitude, longitude, sender_id, remetente_nome, nivel_dificuldade,
                                        volume_caixas, janela_inicio, janela_fim, dados_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rota_id, p.get("ordem"), p.get("service_id"), p.get("codigo"), p.get("titulo"),
            p.get("destinatario_nome"), p.get("endereco"), p.get("latitude"), p.get("longitude"),
            p.get("sender_id"), p.get("remetente_nome"), p.get("nivel_dificuldade"), p.get("volume_caixas"),
            p.get("horario_atendimento_inicio"), p.get("horario_atendimento_fim"), _json(p),
        ))
    return rota_id


def materializar_rascunho(rascunho: dict, provedor: str, vuupt_route_id: int | None = None,
                          conn: sqlite3.Connection | None = None) -> int:
    """Cria a rota + paradas a partir de um rascunho (dict de
    rascunhos_rota.buscar_rascunho). Idempotente por vuupt_route_id: se a
    rota da VUUPT já existe aqui, só devolve o id."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        if vuupt_route_id is not None:
            existente = conn.execute(
                "SELECT id FROM nucleo_rotas WHERE vuupt_route_id = ?", (vuupt_route_id,)
            ).fetchone()
            if existente:
                return existente["id"]
        rota_id = _inserir_rota(conn, rascunho, provedor, vuupt_route_id)
        registrar_evento(
            conn, "ROTA_ENVIADA", banco.ORIGEM_PAINEL, rota_id=rota_id, agent_id=rascunho.get("agent_id"),
            dados={"provedor": provedor, "vuupt_route_id": vuupt_route_id, "rascunho_id": rascunho.get("id")},
        )
        conn.commit()
        return rota_id
    finally:
        if fechar:
            conn.close()


def registrar_rota_enviada(rascunho_id: int, vuupt_route_id: int) -> int | None:
    """Gancho de rascunhos_rota.marcar_enviado (best-effort, o chamador
    embrulha em try/except). Lê o rascunho pelo módulo do painel e
    materializa com provedor=VUUPT."""
    from rascunhos_rota import buscar_rascunho  # painel_agentes/ (sys.path acima)

    rascunho = buscar_rascunho(rascunho_id)
    if not rascunho:
        logger.warning(f"nucleo: rascunho {rascunho_id} não encontrado -- rota VUUPT {vuupt_route_id} não espelhada.")
        return None
    rota_id = materializar_rascunho(rascunho, banco.PROVEDOR_VUUPT, vuupt_route_id)
    logger.info(f"nucleo: rota VUUPT {vuupt_route_id} (rascunho {rascunho_id}) espelhada como nucleo_rotas.id={rota_id}.")
    return rota_id


def marcar_cancelada_por_vuupt_route_id(vuupt_route_id: int, origem: str = banco.ORIGEM_PAINEL,
                                        conn: sqlite3.Connection | None = None) -> bool:
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        row = conn.execute("SELECT id, status FROM nucleo_rotas WHERE vuupt_route_id = ?", (vuupt_route_id,)).fetchone()
        if not row or row["status"] == banco.ROTA_CANCELADA:
            return False
        agora = banco.agora()
        conn.execute("UPDATE nucleo_rotas SET status = ?, cancelada_em = ?, atualizado_em = ? WHERE id = ?",
                     (banco.ROTA_CANCELADA, agora, agora, row["id"]))
        registrar_evento(conn, "ROTA_CANCELADA", origem, rota_id=row["id"])
        conn.commit()
        return True
    finally:
        if fechar:
            conn.close()


# ── Consultas ──────────────────────────────────────────────────────────────────

def _montar_rota(conn: sqlite3.Connection, row: sqlite3.Row, com_paradas: bool) -> dict:
    rota = dict(row)
    rota.pop("dados_json", None)
    if com_paradas:
        paradas = conn.execute(
            "SELECT * FROM nucleo_paradas WHERE rota_id = ? ORDER BY ordem", (row["id"],)
        ).fetchall()
        rota["paradas"] = []
        for p in paradas:
            d = dict(p)
            d.pop("dados_json", None)
            rota["paradas"].append(d)
    return rota


def buscar_rota(rota_id: int, conn: sqlite3.Connection | None = None) -> dict | None:
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        row = conn.execute("SELECT * FROM nucleo_rotas WHERE id = ?", (rota_id,)).fetchone()
        return _montar_rota(conn, row, com_paradas=True) if row else None
    finally:
        if fechar:
            conn.close()


def listar_rotas_dia(data_rota: date | str, provedor: str | None = None, com_paradas: bool = True,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    """Rotas de um dia (todas, ou só de um provedor) -- é o que a Torre
    vai somar à listagem da VUUPT na Fase C."""
    data_txt = data_rota.isoformat() if isinstance(data_rota, date) else str(data_rota)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        sql = "SELECT * FROM nucleo_rotas WHERE data_rota = ?"
        params: list = [data_txt]
        if provedor:
            sql += " AND provedor = ?"
            params.append(provedor)
        sql += " ORDER BY start_at, nome"
        return [_montar_rota(conn, r, com_paradas) for r in conn.execute(sql, params).fetchall()]
    finally:
        if fechar:
            conn.close()


def listar_rotas_motorista(agent_id: int, data_ini: date | str, data_fim: date | str,
                           conn: sqlite3.Connection | None = None) -> list[dict]:
    ini = data_ini.isoformat() if isinstance(data_ini, date) else str(data_ini)
    fim = data_fim.isoformat() if isinstance(data_fim, date) else str(data_fim)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        rows = conn.execute("""
            SELECT * FROM nucleo_rotas
            WHERE agent_id = ? AND data_rota BETWEEN ? AND ?
            ORDER BY data_rota, start_at
        """, (agent_id, ini, fim)).fetchall()
        return [_montar_rota(conn, r, com_paradas=False) for r in rows]
    finally:
        if fechar:
            conn.close()