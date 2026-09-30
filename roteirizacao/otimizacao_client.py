# -*- coding: utf-8 -*-
"""
otimizacao_client.py

Cliente da API PÚBLICA de roteirização do VUUPT — endpoint validado em
produção (31/07): POST https://api.vuupt.com/api/v1/route-optimization.
Domínio DIFERENTE do resto do projeto (api.vuupt.com, não
app.vuupt.com), formato JSON, mesma autenticação Bearer.

Assíncrono: cria com status "waiting", processa em alguns segundos —
usar consultar_otimizacao() em polling até sair de waiting/processing.
"""
import logging
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from http_retry import chamar_com_retry

logger = logging.getLogger(__name__)

API_BASE = "https://api.vuupt.com/api/v1"
STATUS_EM_ANDAMENTO = ("waiting", "processing")


def criar_otimizacao(token: str, base_lat: float, base_lng: float,
                     vehicle_id: str, service_ids: list[str],
                     data_inicio: str, hora_inicio: str = "11:00:00",
                     hora_fim: str = "21:00:00") -> dict:
    """
    Cria uma nova roteirização (POST /route-optimization) pra 1
    veículo e a lista de service_ids dada. Retorna o corpo da resposta
    (inclui "id" e "status", inicialmente "waiting").

    Levanta a exceção original (requests) se a chamada falhar --
    quem chama decide como tratar/logar.
    """
    payload = {
        "config": {
            "startTime": f"{data_inicio} {hora_inicio}",
            "endTime": f"{data_inicio} {hora_fim}",
            "returnToDepot": True,
            "restrictions": {
                "dimensions": True,
                "operatingHour": True,
                "skills": False,
            },
        },
        "vehicles": [
            {
                "id": str(vehicle_id),
                "startLocation": {"lat": base_lat, "lng": base_lng},
                "endLocation": {"lat": base_lat, "lng": base_lng},
                "timeWindow": {
                    "startTime": f"{data_inicio}T{hora_inicio}Z",
                    "endTIme": f"{data_inicio}T{hora_fim}Z",  # sic -- nome real do campo na API
                },
            }
        ],
        "activities": [{"service_id": str(sid)} for sid in service_ids],
    }

    resp = chamar_com_retry(
        requests.post,
        f"{API_BASE}/route-optimization",
        json=payload,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def consultar_otimizacao(token: str, otimizacao_id: int) -> dict:
    """GET /route-optimization/{id} -- status e, quando processado, a
    solução (rotas montadas + serviços não atribuídos)."""
    resp = chamar_com_retry(
        requests.get,
        f"{API_BASE}/route-optimization/{otimizacao_id}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def aguardar_conclusao(token: str, otimizacao_id: int,
                       tentativas: int = 12, intervalo_seg: int = 5) -> dict:
    """
    Faz polling em consultar_otimizacao() até sair de waiting/
    processing, ou até esgotar as tentativas (retorna o último estado
    conhecido nesse caso — quem chama decide como tratar um timeout).
    """
    dados = consultar_otimizacao(token, otimizacao_id)
    for tentativa in range(tentativas):
        status = dados.get("status")
        logger.info(f"  Otimização {otimizacao_id}: tentativa {tentativa + 1}, status={status}")
        if status not in STATUS_EM_ANDAMENTO:
            return dados
        time.sleep(intervalo_seg)
        dados = consultar_otimizacao(token, otimizacao_id)
    logger.warning(f"Otimização {otimizacao_id} não concluiu após {tentativas} tentativas "
                   f"({tentativas * intervalo_seg}s) — status ainda '{dados.get('status')}'.")
    return dados


def otimizar_sequencia_rota(token: str, service_ids: list[int], base_lat: float, base_lng: float,
                            vehicle_id: str, data_referencia: str) -> list[int]:
    """
    Calcula a MELHOR ORDEM de visita pros service_ids dados, usando o
    solver de roteirização (route-optimization, api.vuupt.com) só
    como ferramenta de sequenciamento -- pedido do Hugo, 02/08: "após
    criar a rota, rodar também o otimizador de trajeto para que a
    ordem da rota faça sentido" (rotas criadas via API não passam pelo
    sequenciamento automático que a tela do VUUPT faz sozinha).

    NÃO decide quais pedidos entram na rota (isso já foi decidido
    antes, por roteirizacao_dados.py) -- só reordena os mesmos
    service_ids que já foram dados. Se o solver deixar algum como
    "unassigned" (por restrição de tempo/distância), esses ainda
    assim são incluídos no final da lista -- nenhum pedido pode ficar
    de fora da rota por causa do sequenciamento.

    Se o solver falhar ou não retornar uma solução utilizável, retorna
    a ORDEM ORIGINAL (fallback seguro -- nunca trava a criação da rota
    por causa do sequenciamento).
    """
    if len(service_ids) < 2:
        return list(service_ids)

    try:
        criado = criar_otimizacao(
            token, base_lat, base_lng, vehicle_id, service_ids, data_inicio=data_referencia,
        )
        resultado = aguardar_conclusao(token, criado["id"])
    except Exception as e:
        logger.warning(f"Falha ao calcular sequência otimizada (usando ordem original): {e}")
        return list(service_ids)

    solucao = resultado.get("solution") or {}
    rotas = solucao.get("routes", [])
    if not rotas:
        logger.warning("Otimizador não retornou nenhuma rota na solução -- usando ordem original.")
        return list(service_ids)

    ordenados = [
        int(a["service_id"]) for r in rotas for a in r.get("activities", [])
        if a.get("type_activity") == "service" and a.get("service_id")
    ]
    nao_atribuidos = [int(s["service_id"]) for s in solucao.get("unassigned", []) if s.get("service_id")]

    # garante que TODOS os service_ids originais aparecem no resultado
    # (os "unassigned" do solver vão pro final, sem ficar de fora)
    vistos = set(ordenados) | set(nao_atribuidos)
    faltando = [sid for sid in service_ids if sid not in vistos]

    return ordenados + nao_atribuidos + faltando


def buscar_veiculo_por_code(token: str, code: str) -> dict | None:
    """Busca um veículo pelo campo 'code' (apelido, ex: 'TAPIOCA') --
    pagina todos os veículos até achar, já que a API não parece
    suportar filtro por code diretamente."""
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    page = 1
    while True:
        resp = chamar_com_retry(requests.get, f"{API_BASE}/vehicles", headers=headers,
                                params={"per_page": 100, "page": page}, timeout=15)
        resp.raise_for_status()
        corpo = resp.json()
        for v in corpo.get("data", []):
            if str(v.get("code", "")).strip().lower() == code.strip().lower():
                return v
        pag = corpo.get("meta", {}).get("pagination", {})
        if page >= pag.get("total_pages", page):
            break
        page += 1
    return None
