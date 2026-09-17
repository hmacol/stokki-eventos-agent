# -*- coding: utf-8 -*-
"""
notificacao_entregas/vuupt_entregas.py

Fonte dos pedidos concluídos + canhoto, hoje a Vuupt. É a ÚNICA peça a
trocar na saída da Vuupt (passa a ler nucleo_paradas + nucleo_comprovantes,
ver DOC_EXECUCAO_CLAUDE_SAIDA_VUUPT.md): quem chama só depende do formato
de serviço devolvido aqui.
"""
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import requests

from http_retry import chamar_com_retry
from vuupt_client import API_BASE_URL, VuuptClient

logger = logging.getLogger(__name__)

_RAIZ = Path(__file__).parent.parent
# Mesmo cache de portal_cliente/dados_cliente.baixar_canhoto_pdf: se o
# portal já baixou, reaproveita (e vice-versa).
CANHOTOS_DIR = _RAIZ / "dados" / "canhotos"
INCLUDES = ["checklistAnswers", "customer", "failedReason"]


def buscar_concluidos(token: str, agora_utc: datetime, horas: int) -> list[dict]:
    """Serviços status=done concluídos desde (agora - horas). O filtro da
    API só aceita DATA (YYYY-MM-DD, em UTC como o completed_at), então a
    janela fina fica por conta de regras_entrega.decidir (max_atraso_horas).
    Medido 17/09: ~260 serviços em 2 s."""
    desde = (agora_utc - timedelta(hours=horas)).strftime("%Y-%m-%d")
    return VuuptClient(token).listar_servicos(
        [{"field": "status", "operator": "eq", "value": "done"},
         {"field": "completed_at", "operator": "gte", "value": desde}],
        include=INCLUDES, limite_paginas=30,
    )


def buscar_por_codigo(token: str, codigo: str) -> list[dict]:
    """Serviços concluídos de um código ('PS-1' ou '#PS-1'), pro --pedido
    do modo teste. A Vuupt guarda o code com e sem '#'."""
    limpo = codigo.strip().lstrip("#").upper()
    achados: dict[int, dict] = {}
    for variante in (limpo, f"#{limpo}"):
        for s in VuuptClient(token).listar_servicos(
                [{"field": "code", "operator": "eq", "value": variante}], include=INCLUDES, limite_paginas=2):
            if s.get("status") == "done":
                achados[s["id"]] = s
    return list(achados.values())


def baixar_canhoto_pdf(token: str, checklist_id: int, codigo: str) -> Path | None:
    CANHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    seguro = re.sub(r"[^A-Za-z0-9_-]", "_", (codigo or "").lstrip("#")) or str(checklist_id)
    destino = CANHOTOS_DIR / f"canhoto_{seguro}.pdf"
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    try:
        resp = chamar_com_retry(requests.get, f"{API_BASE_URL}/checklists/{checklist_id}/print",
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/pdf"},
                                timeout=60)
        resp.raise_for_status()
        if not resp.content:
            return None
        destino.write_bytes(resp.content)
        return destino
    except Exception as e:
        logger.warning(f"Canhoto do checklist {checklist_id} ({codigo}) falhou: {e}")
        return None
