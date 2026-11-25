# -*- coding: utf-8 -*-
"""
comprovante_entrega/fotos_vuupt.py

Fotos ORIGINAIS do checklist de um servico na Vuupt (canhoto, nota de
retorno, foto da ocorrencia), pra entrar no comprovante proprio
(gerar_pdf.py). Substitui o GET /checklists/{id}/print, que devolvia o PDF
pronto da Vuupt.

E a peca a trocar na saida da Vuupt: passa a ler nucleo_comprovantes.

GET /checklists?filter entity_id + include=fields devolve, em cada campo com
anexo, um `file_url` assinado que EXPIRA: baixa na hora.
"""
import logging

import requests

from http_retry import chamar_com_retry
from vuupt_client import API_BASE_URL

logger = logging.getLogger(__name__)

# Campos do formulario "Checklist de Entregas com Sucesso" (a API nao devolve o nome).
ROTULOS_CAMPO = {15086: "Canhoto assinado", 15349: "Nota de retorno"}
CAMPO_CANHOTO = 15086
MAX_FOTOS = 4
MAX_BYTES_FOTO = 20 * 1024 * 1024


def _lista(obj) -> list[dict]:
    lista = obj.get("data") if isinstance(obj, dict) else obj
    return [x for x in (lista or []) if isinstance(x, dict)]


def buscar_fotos(token: str, servico: dict, http_get=None) -> list[tuple[str, bytes]]:
    """[(rotulo, conteudo)] das fotos do servico, canhoto primeiro. Falha de
    rede ou foto que nao baixa nunca levanta: devolve o que conseguiu."""
    http_get = http_get or (lambda url, **kw: chamar_com_retry(requests.get, url, **kw))
    service_id = servico.get("id")
    if not service_id:
        return []
    try:
        resp = http_get(f"{API_BASE_URL}/checklists", timeout=30,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                        params={"filter[0][field]": "entity_id", "filter[0][operator]": "eq",
                                "filter[0][value]": service_id, "include": "fields", "per_page": 20})
        resp.raise_for_status()
        checklists = _lista(resp.json())
    except Exception as e:
        logger.warning(f"Checklists do servico {service_id} indisponiveis: {e}")
        return []

    padrao = "Foto da ocorrência" if servico.get("status_done") == "failed" else "Foto da entrega"
    campos = []
    # o filtro pode ser ignorado em silencio pela API: confere o entity_id
    for cl in checklists:
        if str(cl.get("entity_id")) != str(service_id):
            continue
        campos += [f for f in _lista(cl.get("fields")) if f.get("file_url")]
    campos.sort(key=lambda f: f.get("checklist_form_field_id") != CAMPO_CANHOTO)

    fotos: list[tuple[str, bytes]] = []
    for campo in campos[:MAX_FOTOS]:
        try:
            r = http_get(campo["file_url"], timeout=60)
            r.raise_for_status()
            if len(r.content) > MAX_BYTES_FOTO:
                raise ValueError(f"{len(r.content)} bytes")
            fotos.append((ROTULOS_CAMPO.get(campo.get("checklist_form_field_id"), padrao), r.content))
        except Exception as e:
            # a mensagem do requests traz a file_url assinada: loga so tipo e status
            status = getattr(getattr(e, "response", None), "status_code", None)
            logger.warning(f"Foto do checklist do servico {service_id} nao baixou: {type(e).__name__} {status or ''}")
    return fotos
