# -*- coding: utf-8 -*-
"""
Regra unica de edicao de rota ja enviada a Vuupt: quem pode sair da rota,
se a rota ja comecou, trocar motorista/carro e adicionar pedidos.

Sempre le a rota AO VIVO (GET /routes/{id}?include=services): o card da tela
pode estar velho. A Vuupt aceita tirar ate pedido entregue (e ele perde o
route_id, estragando romaneio/km/financeiro), entao quem trava e este modulo.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

import rotas_client

STATUS_PODE_SAIR = frozenset({"not_assigned", "assigned", "accepted"})
STATUS_COMECADO = frozenset({"on_route", "arrived", "done"})
_STATUS_ROTA_COMECADA = frozenset({"started", "finished"})
STATUS_ROTA_ENCERRADA = frozenset({"finished", "canceled"})
_STATUS_ROTA_ENCERRADA = STATUS_ROTA_ENCERRADA


class ErroEdicao(Exception):
    pass


def _rota_do_corpo(corpo) -> dict:
    if not isinstance(corpo, dict):
        return {}
    for chave in ("route", "data"):
        valor = corpo.get(chave)
        if isinstance(valor, dict):
            return valor
    return corpo


def _servicos_do_corpo(rota: dict) -> list[dict]:
    sv = rota.get("services")
    if isinstance(sv, dict):
        return sv.get("data", []) or []
    if isinstance(sv, list):
        return sv
    return []


def ler_rota(token: str, route_id: int) -> dict:
    """Le a rota ao vivo. Levanta ErroEdicao se sumiu (404) ou a consulta falhar."""
    try:
        corpo = rotas_client.buscar_rota(token, route_id, include=["services"])
    except Exception as e:
        resposta = getattr(e, "response", None)
        if resposta is not None and getattr(resposta, "status_code", None) == 404:
            raise ErroEdicao(f"Rota #{route_id} não existe mais na Vuupt.")
        raise ErroEdicao(f"Falha ao consultar a rota #{route_id} na Vuupt: {e}")
    rota = _rota_do_corpo(corpo)
    return {
        "id": rota.get("id", route_id),
        "status": rota.get("status"),
        "agent_id": rota.get("agent_id"),
        "vehicle_id": rota.get("vehicle_id"),
        "servicos": [
            {"id": s.get("id"), "code": s.get("code"), "status": s.get("status"),
             "dimension_3": s.get("dimension_3")}
            for s in _servicos_do_corpo(rota)
        ],
    }


def rota_iniciada(rota: dict) -> bool:
    """Algum servico on_route/arrived/done, ou rota started/finished."""
    if rota.get("status") in _STATUS_ROTA_COMECADA:
        return True
    return any(s.get("status") in STATUS_COMECADO for s in rota.get("servicos", []))


def _recusa_rota_encerrada(rota: dict) -> str | None:
    if rota.get("status") in _STATUS_ROTA_ENCERRADA:
        return (f"Rota #{rota['id']} já foi encerrada (status na Vuupt: '{rota['status']}') "
                f"-- não dá pra alterar.")
    return None


def _erro_status_servico(servico: dict) -> str:
    st = servico.get("status")
    sid = servico.get("id")
    if st in ("on_route", "arrived"):
        return f"Pedido {sid} já começou (status na Vuupt: '{st}') e não pode sair da rota."
    if st == "done":
        return f"Pedido {sid} já foi concluído e não pode sair da rota."
    if st == "canceled":
        return f"Pedido {sid} está cancelado e não pode sair da rota."
    return f"Pedido {sid} com status '{st}' não pode sair da rota."


def tirar_pedido(token: str, route_id: int, service_id: int) -> dict:
    """Tira um pedido pendente da rota (status ao vivo). Se a rota ficaria sem
    nenhum servico e nada comecou, cancela a rota (unassign); senao DELETE."""
    try:
        rota = ler_rota(token, route_id)
    except ErroEdicao as e:
        return {"ok": False, "erro": str(e)}
    erro = _recusa_rota_encerrada(rota)
    if erro:
        return {"ok": False, "erro": erro}

    alvo = next((s for s in rota["servicos"] if s["id"] == service_id), None)
    if alvo is None:
        # fora_da_rota: o card esta velho (outra tela ja tirou); quem chama atualiza o local
        return {"ok": False, "fora_da_rota": True, "erro": f"Pedido {service_id} não está mais nessa rota."}
    if alvo["status"] not in STATUS_PODE_SAIR:
        return {"ok": False, "erro": _erro_status_servico(alvo)}

    iniciada = rota_iniciada(rota)
    sobram = [s for s in rota["servicos"] if s["id"] != service_id]
    cancelar = not sobram and not iniciada
    try:
        if cancelar:
            rotas_client.cancelar_rota(token, route_id, services_action="unassign")
        else:
            rotas_client.remover_servico_da_rota(token, route_id, service_id)
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    return {"ok": True, "rota_cancelada": cancelar, "rota_iniciada": iniciada, "servico": alvo}


def trocar_motorista_veiculo(token: str, route_id: int, agent_id, vehicle_id) -> dict:
    """Troca motorista e/ou carro. Rota iniciada: so o carro (agent_id igual ao atual)."""
    try:
        rota = ler_rota(token, route_id)
    except ErroEdicao as e:
        return {"ok": False, "erro": str(e)}
    erro = _recusa_rota_encerrada(rota)
    if erro:
        return {"ok": False, "erro": erro}
    if rota_iniciada(rota) and agent_id != rota["agent_id"]:
        return {"ok": False, "erro": "Rota já começou: só o carro pode mudar."}
    try:
        nova = rotas_client.atualizar_motorista_veiculo(token, route_id, agent_id, vehicle_id)
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    return {"ok": True, "rota": nova}


def adicionar_pedidos(token: str, route_id: int, service_ids: list[int], position="end") -> dict:
    """Adiciona pedidos na rota; `falharam` traz os ids que a Vuupt recusou."""
    try:
        corpo = rotas_client.adicionar_atividades(token, route_id, service_ids, position=position)
    except Exception as e:
        return {"ok": False, "erro": str(e)}
    falhas = ((corpo or {}).get("data") or {}).get("info", {}).get("failed") or []
    return {"ok": True, "falharam": [f.get("id") if isinstance(f, dict) else f for f in falhas]}
