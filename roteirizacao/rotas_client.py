# -*- coding: utf-8 -*-
"""
rotas_client.py

Cliente para o endpoint de ROTAS de verdade do VUUPT (documentação
pública, 01/08 -- diferente do route-optimization, que só CALCULA uma
solução sem materializar nada. Confirmado em produção: pedidos
"roteirizados" via route-optimization continuavam not_assigned no
VUUPT. Este é o endpoint que de fato CRIA/ALTERA rotas reais):

    POST   https://api.vuupt.com/api/v1/routes                 -- criar rota
    GET    https://api.vuupt.com/api/v1/routes                 -- listar
    GET    https://api.vuupt.com/api/v1/routes/{id}             -- detalhe
    PUT    https://api.vuupt.com/api/v1/routes/{id}             -- atualizar
    DELETE https://api.vuupt.com/api/v1/routes/{id}             -- excluir
    POST   https://api.vuupt.com/api/v1/routes/{id}/activities  -- adicionar pedidos numa rota existente
    DELETE https://api.vuupt.com/api/v1/routes/{id}/cancel      -- cancelar

Mesma autenticação Bearer usada em todo o projeto. Domínio api.vuupt.com
(diferente do app.vuupt.com usado pelo resto do agente_stokki_eventos).
"""
import logging
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from http_retry import chamar_com_retry

logger = logging.getLogger(__name__)

API_BASE = "https://api.vuupt.com/api/v1"

PADRAO_CONFLITO_ROTA = re.compile(
    r"j[aá] faz parte de uma rota.*?(PS-\d+)", re.IGNORECASE | re.DOTALL
)


def _headers(token: str) -> dict:
    return {"Accept": "application/json", "Authorization": f"Bearer {token}"}


def _verificar_resposta(resp):
    """
    Como resp.raise_for_status() sozinho, mas inclui o CORPO da
    resposta na mensagem de erro -- achado em produção, 06/08: 4
    rotas seguidas falhando com "400 Client Error: Bad Request" sem
    dar pra saber o motivo real (raise_for_status() não inclui o
    corpo, só o status code genérico). Não faz nada se a resposta
    estiver OK.
    """
    if resp.ok:
        return
    try:
        corpo_erro = resp.json()
    except Exception:
        corpo_erro = resp.text
    raise requests.exceptions.HTTPError(
        f"{resp.status_code} {resp.reason} for url {resp.url} -- corpo da resposta: {corpo_erro}",
        response=resp,
    )


def criar_rota(token: str, nome: str, start_at: str, start_location_base_id: int,
               service_ids: list[int], end_location_type: str = "operational_base",
               end_location_base_id: int | None = None, agent_id: int | None = None,
               vehicle_id: int | None = None, transport_mode: str = "driving") -> dict:
    """
    Cria uma rota de verdade no VUUPT com os pedidos dados, na ordem
    dada (o endpoint NÃO otimiza a sequência sozinho — a ordem de
    service_ids é a ordem final da rota).

    agent_id/vehicle_id OMITIDOS por padrão (None) -- rota fica sem
    veículo atribuído, pra atribuição manual depois (pedido do Hugo,
    01/08). end_location_base_id só é obrigatório quando
    end_location_type='operational_base' (padrão).
    """
    payload = {
        "name": nome,
        "start_at": start_at,
        "start_location_base_id": start_location_base_id,
        "end_location_type": end_location_type,
        "transport_mode": transport_mode,
        "activities": [{"type": "service", "service_id": sid} for sid in service_ids],
    }
    if end_location_base_id is not None:
        payload["end_location_base_id"] = end_location_base_id
    if agent_id is not None:
        payload["agent_id"] = agent_id
    if vehicle_id is not None:
        payload["vehicle_id"] = vehicle_id

    resp = chamar_com_retry(requests.post, f"{API_BASE}/routes", json=payload, headers=_headers(token), timeout=30)
    _verificar_resposta(resp)
    return resp.json()["route"]


def criar_rota_removendo_conflitos(token: str, nome_rota: str, start_at: str,
                                   sublote: list[dict], start_location_base_id: int,
                                   end_location_base_id: int | None = None,
                                   max_tentativas: int = 8,
                                   agent_id: int | None = None, vehicle_id: int | None = None):
    """
    Cria a rota com o sublote dado -- se o VUUPT recusar por algum
    serviço já estar em OUTRA rota (achado em produção, 06/08: a lista
    'not_assigned' pode ficar levemente desatualizada entre a busca e
    a criação de fato, e a API rejeita o POST INTEIRO por causa de 1
    serviço só, perdendo os outros 10-15 pedidos legítimos do lote
    junto), remove só o(s) serviço(s) apontado(s) no erro e tenta de
    novo -- não perde o lote inteiro por causa de 1 entrada obsoleta.

    `sublote` é uma lista de dicts com pelo menos as chaves "id"
    (service_id) e "code" (código do pedido, ex: "PS-12345" -- usado
    só pra casar com o código apontado na mensagem de erro). Usada
    tanto pelo pipeline automático (roteirizacao/criar_rotas_diarias.py)
    quanto pelo envio manual de rascunhos aprovados na tela de
    planejamento (painel_agentes/painel_agentes.py) -- extraída daqui
    pra não duplicar a lógica de regex+retry nos dois lugares.

    Retorna (rota_criada_ou_None, sublote_final_usado, codigos_removidos).
    Se sobrar 0 serviços ou passar de max_tentativas, retorna
    (None, [], codigos_removidos) -- quem chama decide como logar.
    """
    sublote_atual = list(sublote)
    codigos_removidos = []

    for _ in range(max_tentativas):
        if not sublote_atual:
            return None, [], codigos_removidos

        service_ids = [s["id"] for s in sublote_atual]
        try:
            rota = criar_rota(
                token, nome=nome_rota, start_at=start_at,
                start_location_base_id=start_location_base_id, service_ids=service_ids,
                end_location_base_id=end_location_base_id,
                agent_id=agent_id, vehicle_id=vehicle_id,
            )
            return rota, sublote_atual, codigos_removidos
        except Exception as e:
            match = PADRAO_CONFLITO_ROTA.search(str(e))
            if not match:
                raise  # erro de outro tipo -- não sabemos remediar, propaga como antes

            codigo_conflitante = match.group(1)
            antes = len(sublote_atual)
            sublote_atual = [s for s in sublote_atual if s.get("code", "").lstrip("#") != codigo_conflitante]
            if len(sublote_atual) == antes:
                # o código apontado no erro não bate com nenhum do sublote atual
                # (já deve ter sido removido numa tentativa anterior, ou algo
                # mudou) -- evita loop infinito reenviando o mesmo pedido
                raise
            codigos_removidos.append(codigo_conflitante)
            logger.warning(f"  '{nome_rota}': {codigo_conflitante} já está em outra rota (dado desatualizado) "
                           f"-- removendo do lote e tentando de novo com os {len(sublote_atual)} restante(s).")

    raise Exception(f"Excedeu {max_tentativas} tentativas removendo conflitos -- "
                    f"removidos até agora: {codigos_removidos}")


def buscar_rota(token: str, route_id: int, include: list[str] | None = None) -> dict:
    """GET /routes/{id}. `include` pode ter: activities, agent, services,
    vehicle, startLocationBase, endLocationBase, sourceRoute, polyline."""
    params = {}
    if include:
        params["include"] = ",".join(include)
    resp = chamar_com_retry(requests.get, f"{API_BASE}/routes/{route_id}", headers=_headers(token), params=params, timeout=30)
    _verificar_resposta(resp)
    return resp.json()


def listar_rotas(token: str, include: list[str] | None = None, per_page: int = 100,
                 filtro: list[dict] | None = None) -> list[dict]:
    """GET /routes, paginando até trazer tudo.

    filtro: lista de dicts {"field","operator","value"} (mesmo formato
    já usado em vuupt_client.py pra /services). CORRIGIDO 06/08 (achado
    em produção, pedido do Hugo): a versão anterior mandava
    params["filter"] = filtro (a lista crua) -- o requests serializa
    isso errado (vira "filter=field&filter=operator&filter=value...",
    sem sentido nenhum), a API simplesmente IGNORA um filtro inválido
    desse jeito e devolve o histórico INTEIRO sem filtrar nada. Isso
    nunca funcionou de verdade, nem aqui nem em mapa_rotas.py (que já
    usava esse parâmetro achando que estava filtrando) -- agora usa o
    formato filter[N][campo]=valor que a API de fato entende.
    """
    params = {"per_page": per_page}
    if include:
        params["include"] = ",".join(include)
    if filtro:
        for i, f in enumerate(filtro):
            params[f"filter[{i}][field]"] = f["field"]
            params[f"filter[{i}][operator]"] = f["operator"]
            params[f"filter[{i}][value]"] = f["value"]

    todas = []
    page = 1
    while True:
        params["page"] = page
        resp = chamar_com_retry(requests.get, f"{API_BASE}/routes", headers=_headers(token), params=params, timeout=30)
        _verificar_resposta(resp)
        corpo = resp.json()
        todas.extend(corpo.get("data", []))
        pag = corpo.get("meta", {}).get("pagination", {})
        if page >= pag.get("total_pages", page):
            break
        page += 1
    return todas


def atualizar_rota(token: str, route_id: int, service_ids_ordenados: list[int]) -> dict:
    """
    PUT /routes/{id} -- substitui a lista de activities pela ordem dada
    (usado pra aplicar a sequência calculada pelo otimizador de
    trajeto, ver roteirizacao_dados.py::otimizar_sequencia_rota).
    ATENÇÃO: informar 'activities' aqui substitui TODOS os serviços da
    rota pela lista dada -- os service_ids_ordenados precisam conter
    TODOS os pedidos que já estavam na rota, só que na ordem nova.
    """
    payload = {"activities": [{"type": "service", "service_id": sid} for sid in service_ids_ordenados]}
    resp = chamar_com_retry(requests.put, f"{API_BASE}/routes/{route_id}", json=payload, headers=_headers(token), timeout=30)
    _verificar_resposta(resp)
    return resp.json()["route"]


def adicionar_atividades(token: str, route_id: int, service_ids: list[int],
                         position: str = "optimize") -> dict:
    """
    POST /routes/{id}/activities -- adiciona pedidos numa rota já
    existente. position: "start", "end", "optimize" (VUUPT decide a
    melhor posição pra inserir cada serviço), ou um número (posição
    específica na fila).

    Retorna o corpo completo da resposta ({"success", "message",
    "data": {"route": {...}, "info": {"failed": [...]}}}). Quem chama
    deve checar data.info.failed pra ver se algum serviço não pôde ser
    adicionado (ex: rota cheia, serviço já atribuído em outro lugar).
    """
    payload = {
        "activities": [{"type": "service", "service_id": sid} for sid in service_ids],
        "position": position,
    }
    resp = chamar_com_retry(requests.post, f"{API_BASE}/routes/{route_id}/activities", json=payload,
                            headers=_headers(token), timeout=30)
    _verificar_resposta(resp)
    return resp.json()


def cancelar_rota(token: str, route_id: int, services_action: str = "keep") -> dict | None:
    """DELETE /routes/{id}/cancel. services_action: "keep" (mantém status
    dos serviços), "unassign" (desatribui), "cancel" (cancela os
    serviços também)."""
    resp = chamar_com_retry(
        requests.delete,
        f"{API_BASE}/routes/{route_id}/cancel",
        headers={**_headers(token), "Content-Type": "application/x-www-form-urlencoded"},
        data={"services_action": services_action},
        timeout=30,
    )
    _verificar_resposta(resp)
    return resp.json() if resp.text else None
