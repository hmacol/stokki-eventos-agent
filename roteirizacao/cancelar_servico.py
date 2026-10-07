# -*- coding: utf-8 -*-
"""
roteirizacao/cancelar_servico.py

Cancela um servico na Vuupt onde quer que ele esteja (pool, rascunho local
ou rota ja enviada), do jeito que o botao "Cancelar pedido" do Planejamento
faz (painel_agentes/planejamento_rotas.cancelar_pedido), mas chamavel de
fora do painel -- usado pelo worker do portal do cliente
(portal_cliente/cancelamento.py). O Planejamento NAO foi alterado
(escopo B da DOC_EXECUCAO_CLAUDE_CANCELAMENTO_PORTAL.md).

Sempre PUT /services/{id}/cancel (status 'canceled'): DELETE faz o
pipeline recriar o pedido ainda aberto na Stokki (memoria
project_cancelar_pedido_delete_recria).
"""
import logging
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "painel_agentes"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import rascunhos_rota  # noqa: E402  (painel_agentes/)
from nucleo.sincronizar_servicos_vuupt import ressincronizar_ids  # noqa: E402
from vuupt_client import VuuptAPIError, VuuptClient  # noqa: E402

logger = logging.getLogger(__name__)


def _conectar_rascunhos() -> sqlite3.Connection:
    conn = sqlite3.connect(rascunhos_rota.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def rascunho_do_servico(service_id: int) -> tuple[int | None, str | None]:
    """(rascunho_id, status) do rascunho vivo mais recente que contem o
    servico; (None, None) se esta no pool."""
    conn = _conectar_rascunhos()
    try:
        row = conn.execute("""
            SELECT r.id, r.status FROM rascunhos_parada p JOIN rascunhos_rota r ON r.id = p.rascunho_id
            WHERE p.service_id = ? AND r.status != ? ORDER BY r.id DESC LIMIT 1
        """, (service_id, rascunhos_rota.STATUS_DESCARTADO)).fetchone()
        return (row["id"], row["status"]) if row else (None, None)
    finally:
        conn.close()


def cancelar_servico_completo(token: str, service_id: int, vuupt=None) -> dict:
    """Devolve {"ok", "ja_estava", "erro"}. `vuupt` injetavel pra teste."""
    vuupt = vuupt or VuuptClient(token)
    try:
        atual = vuupt.buscar_servico_por_id(service_id) or {}
    except VuuptAPIError as e:
        return {"ok": False, "ja_estava": False, "erro": f"Vuupt nao respondeu o servico {service_id}: {e}"}
    if atual.get("status") == "canceled":
        return {"ok": True, "ja_estava": True, "erro": ""}

    rascunho_id, status_rascunho = rascunho_do_servico(service_id)
    if rascunho_id and status_rascunho == rascunhos_rota.STATUS_ENVIADO:
        preparo = rascunhos_rota.preparar_cancelamento_de_parada(rascunho_id, service_id, token)
        if not preparo.get("ok"):
            return {"ok": False, "ja_estava": False, "erro": preparo.get("erro") or "rota enviada nao liberou a parada"}

    try:
        vuupt.cancelar_servico_oficial(service_id)
    except VuuptAPIError as e:
        return {"ok": False, "ja_estava": False, "erro": f"Vuupt: {e}"}

    if rascunho_id and status_rascunho != rascunhos_rota.STATUS_ENVIADO:
        rascunhos_rota.remover_parada(rascunho_id, service_id)
    try:
        ressincronizar_ids(vuupt, [service_id])
    except Exception as e:  # noqa: BLE001 -- a escrita na Vuupt ja aconteceu; o timer de 15 min alcanca
        logger.warning(f"ressincronizacao do servico {service_id} falhou: {e}")
    return {"ok": True, "ja_estava": False, "erro": ""}
