# -*- coding: utf-8 -*-
"""
mapa_rotas.py

Busca as rotas reais do VUUPT (com os pedidos de cada uma, já na
ordem que serão visitados) e monta os dados pro mapa -- pedido do
Hugo, 04/08: "visualizar o caminho no mapa que seria percorrido pelo
carro pelas geolocalizações que foram definidas".

Lapidado 05/08 (pedido do Hugo):
  - título do serviço + nome do remetente em cada parada, pra filtrar
  - células da grade geográfica usada por agrupar_por_regiao()
    (roteirizacao_dados.py) desenhadas no mapa -- a MESMA lógica de
    arredondamento, reaproveitada aqui pra mostrar visualmente como o
    agrupamento de rotas decide "quem é vizinho de quem"

Reaproveita roteirizacao/rotas_client.py (listar_rotas) e a mesma
lógica de desembrulhar services.data já usada em incrementar_rotas.py.
"""
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from rotas_client import listar_rotas
from geocodificacao import geocodificar
from mapa_util import carregar_remetentes_por_sender_id, celula_grade, extrair_servicos_da_rota

logger = logging.getLogger(__name__)

ENDERECO_BASE = "Rua Zilda, 288, Casa Verde Alta, São Paulo"
PREFIXO_NOME_ROTA = "Planejamento"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def buscar_rotas_para_mapa(data_alvo: date | None = None) -> dict:
    """
    Busca as rotas de uma data (padrão: amanhã, mesma data que
    criar_rotas_diarias.py normalmente planeja) com seus pedidos já na
    ordem de visita, prontas pro mapa.

    Retorna {"base","data_alvo","rotas":[{"nome","paradas":[...]}],
    "regioes":[{"chave","lat_min"...}], "remetentes":[nomes ordenados],
    "google_maps_key"} -- cada parada com lat/lng/nome/endereco/titulo/
    remetente. Rota sem nenhum serviço com coordenada é descartada.
    """
    t_total = time.perf_counter()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    t0 = time.perf_counter()
    remetentes_por_id = carregar_remetentes_por_sender_id()
    logger.info(f"[mapa-rotas] carregar remetentes: {time.perf_counter() - t0:.2f}s ({len(remetentes_por_id)} remetente(s))")

    data_alvo = data_alvo or (date.today() + timedelta(days=1))
    data_br = data_alvo.strftime("%d/%m/%Y")
    prefixo = f"{PREFIXO_NOME_ROTA} - {data_br}"

    t0 = time.perf_counter()
    coords_base = geocodificar(ENDERECO_BASE, gmaps_key)
    logger.info(f"[mapa-rotas] geocodificar base: {time.perf_counter() - t0:.2f}s")

    # Filtra por start_at direto na API (mesmo formato de filtro já
    # confirmado funcionando pra /services em outros pontos do
    # projeto) -- evita baixar o HISTÓRICO inteiro de rotas (achado
    # 05/08: mais de 7 mil rotas acumuladas) só pra usar um punhado.
    # Se o filtro não for suportado por /routes especificamente (API
    # devolve vazio ou dá erro), cai pro jeito antigo -- mais lento,
    # mas nunca mostra "nenhuma rota" por engano.
    inicio_dia = data_alvo.strftime("%Y-%m-%d") + " 00:00:00"
    fim_dia = (data_alvo + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    filtro_data = [
        {"field": "start_at", "operator": "gte", "value": inicio_dia},
        {"field": "start_at", "operator": "lt", "value": fim_dia},
    ]

    t0 = time.perf_counter()
    try:
        rotas_filtradas = listar_rotas(token, include=["services"], filtro=filtro_data)
        rotas_do_dia = [r for r in rotas_filtradas if r.get("name", "").startswith(prefixo)]
        logger.info(f"[mapa-rotas] listar_rotas COM filtro: {time.perf_counter() - t0:.2f}s "
                   f"({len(rotas_filtradas)} rota(s) recebida(s), {len(rotas_do_dia)} bateram com o dia pedido)")
    except Exception as e:
        logger.warning(f"[mapa-rotas] Filtro por start_at não funcionou em /routes ({e}) -- "
                       f"caindo pro histórico completo (mais lento).")
        t1 = time.perf_counter()
        todas_rotas = listar_rotas(token, include=["services"])
        rotas_do_dia = [r for r in todas_rotas if r.get("name", "").startswith(prefixo)]
        logger.info(f"[mapa-rotas] listar_rotas SEM filtro (fallback): {time.perf_counter() - t1:.2f}s "
                   f"({len(todas_rotas)} rota(s) no histórico completo)")

    t0 = time.perf_counter()

    rotas_processadas = []
    regioes_por_chave: dict[str, dict] = {}
    remetentes_vistos: set[str] = set()

    for rota in rotas_do_dia:
        servicos = extrair_servicos_da_rota(rota)
        paradas = []
        for s in servicos:
            lat, lng = s.get("latitude"), s.get("longitude")
            if not lat or not lng:
                continue
            try:
                lat_f, lng_f = float(lat), float(lng)
            except (TypeError, ValueError):
                continue

            remetente = remetentes_por_id.get(s.get("sender_id"), "Remetente não identificado")
            remetentes_vistos.add(remetente)

            paradas.append({
                "lat": lat_f, "lng": lng_f,
                "nome": s.get("code", ""), "endereco": s.get("address", ""),
                "titulo": s.get("title", ""), "remetente": remetente,
            })

            celula = celula_grade(lat_f, lng_f)
            if celula["chave"] not in regioes_por_chave:
                regioes_por_chave[celula["chave"]] = {**celula, "qtd_paradas": 0}
            regioes_por_chave[celula["chave"]]["qtd_paradas"] += 1

        if not paradas:
            continue
        rotas_processadas.append({
            "id": rota["id"], "nome": rota.get("name", ""), "paradas": paradas,
        })

    rotas_processadas.sort(key=lambda r: r["nome"])
    logger.info(f"[mapa-rotas] processar paradas/regiões: {time.perf_counter() - t0:.2f}s "
               f"({len(rotas_processadas)} rota(s) com parada geolocalizada)")
    logger.info(f"[mapa-rotas] TOTAL: {time.perf_counter() - t_total:.2f}s")

    return {
        "base": {"lat": coords_base[0], "lng": coords_base[1]} if coords_base else None,
        "data_alvo": data_br,
        "rotas": rotas_processadas,
        "regioes": list(regioes_por_chave.values()),
        "remetentes": sorted(remetentes_vistos),
        "google_maps_key": gmaps_key,
    }
