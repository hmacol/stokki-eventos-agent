# -*- coding: utf-8 -*-
"""
portal_cliente/cancelamento.py

Executa, no worker (enviar_stokki.ciclo), os cancelamentos que o cliente
pediu no portal (portal_envios.status = CANCELANDO) -- ver
DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md.

Regra "pode cancelar sozinho" (decidir): lida do núcleo, sem chamar a
Vuupt. Precisa da operação se algum serviço vivo já foi ENTREGUE /
INSUCESSO ou se a rota dele já começou (nucleo_rotas EM_ROTA / started /
iniciada_em) ou é da Lalamove. Atenção: nucleo_pedidos.status = EM_ROTA
significa só "atribuído a uma rota" (status_provedor = assigned).

Três fins: CANCELADO (as duas pontas), CRIADO + solicitação PENDENTE +
aviso (precisa da operação ou falha técnica). Nunca fica tentando sozinho.
"""
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_AQUI = Path(__file__).parent
_RAIZ = _AQUI.parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402

logger = logging.getLogger("portal_cancelamento")

SOZINHO = "SOZINHO"
OPERACAO = "OPERACAO"
_RE_PS = re.compile(r"PS-(\d+)")
_ROTA_COMECOU = ("EM_ROTA", "CONCLUIDA")
_PROVEDOR_ROTA_COMECOU = ("started", "finished")


def id_stokki_do_codigo(codigo) -> int | None:
    m = _RE_PS.search(str(codigo or "").upper())
    return int(m.group(1)) if m else None


def _vivos(servicos: list[dict]) -> list[dict]:
    return [s for s in servicos if not s.get("excluido_em")
            and s.get("status") != "CANCELADO" and s.get("status_provedor") != "canceled"]


def decidir(servicos: list[dict], rotas: dict[int, dict], agent_lalamove: int = 0) -> tuple[str, str]:
    """(SOZINHO | OPERACAO, motivo em texto curto)."""
    vivos = _vivos(servicos)
    if not vivos:
        return SOZINHO, "sem serviço vivo na Vuupt"
    for s in vivos:
        if s.get("status") == "ENTREGUE":
            return OPERACAO, f"pedido já entregue ({s['codigo']})"
        if s.get("status") == "INSUCESSO":
            return OPERACAO, f"insucesso em tratamento ({s['codigo']})"
        rota = rotas.get(s.get("vuupt_route_id")) if s.get("vuupt_route_id") else None
        if rota:
            if agent_lalamove and rota.get("agent_id") == agent_lalamove:
                return OPERACAO, f"rota Lalamove ({s['codigo']}): cancelar na Lalamove à mão"
            if rota.get("status") in _ROTA_COMECOU or rota.get("status_provedor") in _PROVEDOR_ROTA_COMECOU or rota.get("iniciada_em"):
                return OPERACAO, f"motorista em rota ({s['codigo']}, rota {s['vuupt_route_id']})"
    return SOZINHO, "pool, rascunho ou rota não iniciada"


def servicos_do_pedido(conn: sqlite3.Connection, codigo_base: str) -> tuple[list[dict], dict[int, dict]]:
    """Linhas de nucleo_pedidos do pedido-base e reentregas (-R1, -C1...) e
    as rotas delas. Tabelas podem não existir no banco local de teste."""
    try:
        rows = conn.execute("""
            SELECT codigo, vuupt_service_id, status, status_provedor, vuupt_route_id, excluido_em
            FROM nucleo_pedidos WHERE codigo = ? OR codigo LIKE ? OR codigo LIKE ?
        """, (codigo_base, f"{codigo_base}-R%", f"{codigo_base}-C%")).fetchall()
    except sqlite3.OperationalError:
        return [], {}
    servicos = [dict(r) for r in rows]
    ids = [s["vuupt_route_id"] for s in servicos if s.get("vuupt_route_id")]
    rotas = {}
    if ids:
        marcas = ",".join("?" * len(ids))
        for r in conn.execute(f"SELECT id, status, status_provedor, iniciada_em, agent_id FROM nucleo_rotas WHERE id IN ({marcas})", ids):
            rotas[r["id"]] = dict(r)
    return servicos, rotas


def _voltar_pra_operacao(conn, envio: dict, agora: str) -> None:
    conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (ep.STATUS_CRIADO, agora, envio["id"]))
    conn.commit()


def _concluir(conn, envio: dict, agora: str, quando: datetime) -> None:
    sol = ep.buscar_solicitacao_cancelamento(conn, envio["id"])
    conn.execute("UPDATE portal_envios SET status = ?, atualizado_em = ? WHERE id = ?", (ep.STATUS_CANCELADO, agora, envio["id"]))
    if sol:
        conn.execute("UPDATE portal_solicitacoes SET status = 'CONCLUIDA', resposta = ?, concluido_em = ? WHERE id = ?",
                     (f"cancelado automaticamente em {quando.strftime('%d/%m %H:%M')}", agora, sol["id"]))
    ep._remover_dedicado(conn, envio["id"], "cancelamento-automatico")
    conn.commit()


def processar_cancelamentos(conn: sqlite3.Connection, config: dict, cancelar_vuupt, cancelar_stokki, avisar,
                            agora: datetime | None = None) -> dict:
    """`cancelar_vuupt(service_id)` e `cancelar_stokki(id_stokki, motivo)`
    devolvem {"ok", "erro"}; `avisar(envio, motivo, erro)` manda e-mail +
    WhatsApp. Injetados pra teste; o worker passa os reais."""
    quando = agora or datetime.now()
    agora_txt = quando.strftime("%Y-%m-%d %H:%M:%S")
    agent_lalamove = int((config.get("lalamove") or {}).get("agent_id_vuupt") or 0)
    total = {"cancelados": 0, "operacao": 0, "falhas": 0}
    rows = conn.execute("SELECT * FROM portal_envios WHERE status = ? ORDER BY id", (ep.STATUS_CANCELANDO,)).fetchall()
    for r in rows:
        envio = dict(r)
        sol = ep.buscar_solicitacao_cancelamento(conn, envio["id"]) or {}
        motivo_cliente = sol.get("detalhes") or ""
        rotulo = ep.rotulo_envio(envio)
        id_stokki = id_stokki_do_codigo(envio.get("codigo_pedido"))
        if not id_stokki:
            _voltar_pra_operacao(conn, envio, agora_txt)
            avisar(envio, "código do pedido ainda não identificado", "")
            total["operacao"] += 1
            continue

        servicos, rotas = servicos_do_pedido(conn, f"PS-{id_stokki}")
        decisao, motivo = decidir(servicos, rotas, agent_lalamove)
        if decisao == OPERACAO:
            logger.info(f"{rotulo} PS-{id_stokki}: precisa da operação ({motivo}).")
            _voltar_pra_operacao(conn, envio, agora_txt)
            avisar(envio, motivo, "")
            total["operacao"] += 1
            continue

        erro = ""
        for s in _vivos(servicos):
            res = cancelar_vuupt(s["vuupt_service_id"])
            if not res.get("ok"):
                erro = f"Vuupt ({s['codigo']}): {res.get('erro')}"
                break
        if not erro:
            res = cancelar_stokki(id_stokki, motivo_cliente)
            if not res.get("ok"):
                erro = f"Stokki: {res.get('erro')}"
        if erro:
            logger.warning(f"{rotulo} PS-{id_stokki}: cancelamento falhou -- {erro}")
            _voltar_pra_operacao(conn, envio, agora_txt)
            avisar(envio, "falha técnica no cancelamento automático", erro)
            total["falhas"] += 1
            continue

        _concluir(conn, envio, agora_txt, quando)
        logger.info(f"{rotulo} PS-{id_stokki}: cancelado na Vuupt e na Stokki.")
        total["cancelados"] += 1
    return total
