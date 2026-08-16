# -*- coding: utf-8 -*-
"""
laboratorio_rotas.py

Laboratório de comparação visual de esquemas de roteirização (pedido do
Hugo, 14/08: "quero poder testar vários tipos de roteirização até
definir qual a melhor. Por geolocalização, por CEP, e qualquer outro
esquema que exista") -- roda os MESMOS pedidos do dia através de 5
esquemas de agrupamento (Atual/Grade+Greedy, Sweep Polar, Clarke-Wright,
CEP real, K-means geográfico -- ver roteirizacao/otimizacao_rotas.py) e
devolve TODOS pro Hugo comparar lado a lado no mapa e numa tabela de
métricas, em vez da seleção automática e silenciosa de
selecao_modelo.py::escolher_melhor_modelo (que continua rodando em
produção, intocada -- ver otimizacao_rotas.py::avaliar_candidatos).

100% leitura: nenhuma rota é criada na VUUPT, nenhum rascunho é gravado,
nenhuma linha entra em selecao_modelo_historico.txt (só escrito por
escolher_melhor_modelo, nunca chamada aqui).
"""
import logging
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import yaml

from vuupt_client import VuuptClient
from geocodificacao import geocodificar
from roteirizacao_dados import elegivel_para_data, particionar_por_macro_regiao, _cache_coordenadas, caixas_e_enderecos
from otimizacao_rotas import (
    agrupar_por_sweep, agrupar_por_savings, agrupar_por_cep, agrupar_por_kmeans,
    avaliar_candidatos,
)
from selecao_modelo import agrupar_atual
from alocacao_motoristas import classificar_rota_viagem
from regras.complexidade_entrega import carregar_niveis, classificar_nivel
from regras.tipo_carga_embarcador import carregar_tipos_carga_por_sender, classificar_tipo_carga, TIPOS_CARGA_FRIA
from regras.tipo_veiculo import classificar_tipo_veiculo
from mapa_util import carregar_remetentes_por_sender_id

logger = logging.getLogger(__name__)

PARTICOES_VALIDAS = ("Seco", "Refrigerado/Congelado")


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _injetar_classificacoes(servicos: list[dict], config: dict, db_path) -> None:
    """Mesmo bloco de criar_rotas_diarias.main()/roteirizar_para_rascunhos
    (nível de dificuldade + tipo de carga, necessários pras 4 travas de
    agrupamento) -- já duplicado nesses dois pontos e em
    benchmark_modelos.py::_injetar_classificacoes; uma 4ª cópia aqui
    evita importar benchmark_modelos.py (tem logging.basicConfig/mkdir
    de nível de módulo) dentro do processo Flask."""
    caminho_niveis = config.get("complexidade_entrega", {}).get("planilha", "")
    mapa_niveis = carregar_niveis(caminho_niveis)
    mapa_tipos_carga = carregar_tipos_carga_por_sender(db_path)
    for s in servicos:
        cnpj_destino = (s.get("customer") or {}).get("code", "")
        nivel, _, _ = classificar_nivel(cnpj_destino, mapa_niveis)
        s["_nivel_dificuldade"] = nivel
        tipo_carga, _ = classificar_tipo_carga(s.get("sender_id"), mapa_tipos_carga)
        s["_tipo_carga"] = tipo_carga


def _gerar_massa_de_teste(gmaps_key: str | None) -> list[dict]:
    """
    Massa de pedidos FICTÍCIOS pro Hugo testar o laboratório sem tocar
    no VUUPT (pedido do Hugo, 14/08 -- criar pedidos de teste de
    verdade no VUUPT arriscava eles serem varridos pelos jobs
    automáticos das 18h/22h antes de serem cancelados, já que nenhum
    deles filtra por remetente/código -- ver incrementar_rotas.py, que
    aloca pedido novo direto numa rota JÁ ENVIADA, sem revisão manual).

    Design (56 pedidos, mesma ordem de grandeza de um dia real):
      - 5 clusters geográficos (testam se o agrupamento fica compacto);
      - 6 pontos espalhados sem padrão (testam o cenário "difícil" que
        motivou o laboratório -- rotas espalhadas);
      - 2 pontos bem longe (Sorocaba/Campinas -- testam a trava de
        macro-região, que nenhuma rota pode cruzar);
      - 2 sem coordenada resolvível, um com CEP reconhecível e outro
        sem nenhum (testam que nenhum modelo perde pedido nesses 2
        fallbacks);
      - casos de trava de negócio embutidos: 1 nível 4 (rota
        exclusiva), 2 nível 3 (limita a rota a 4 entregas), 1 pedido
        "gigante" de caixas (também rota exclusiva, e também já
        classifica sozinho como veículo grande -- ver regras/
        tipo_veiculo.py);
      - 4 pedidos moderados em 4 endereços diferentes e próximos, que
        somados cruzam o piso de veículo grande sem nenhum ser
        "gigante" individualmente (testa a consolidação em 1 rota
        exclusiva de VAN/HR, ver separar_pedidos_exclusivos::
        _extrair_grupos_veiculo_grande).

    Tudo com "[TESTE]" no código/título/endereço -- impossível
    confundir com pedido de verdade em qualquer tela do painel.

    Coordenadas são gravadas em DOIS lugares (nenhuma chamada de rede,
    funciona mesmo sem google_maps.api_key configurada):
      1. roteirizacao_dados._cache_coordenadas -- é o que obter_
         coordenadas() consulta, usado por TODOS os agrupamentos;
      2. campos "latitude"/"longitude" no próprio dict -- é o que
         _sublote_para_mapa() (e o atalho rápido de ordenar_2opt/
         ordenar_por_distancia_base) leem direto, mesmo formato dos
         serviços reais da VUUPT.
    """
    clusters = [
        # (nome, lat, lng, cep_prefixo, quantidade, tipo_carga, cidade)
        ("Casa Verde",  -23.510, -46.660, "02510", 10, "Seco",        "Sao Paulo"),
        ("Santo Amaro", -23.650, -46.710, "04750", 10, "Seco",        "Sao Paulo"),
        ("Tatuape",     -23.540, -46.580, "03310", 8,  "Refrigerado", "Sao Paulo"),
        ("Pinheiros",   -23.565, -46.700, "05422", 8,  "Seco",        "Sao Paulo"),
        ("Santo Andre", -23.660, -46.530, "09080", 6,  "Congelado",   "Santo Andre"),
    ]
    espalhados = [
        # (lat, lng, cep, tipo_carga)
        (-23.430, -46.750, "07000", "Seco"),
        (-23.720, -46.650, "04870", "Seco"),
        (-23.500, -46.400, "08230", "Refrigerado"),
        (-23.620, -46.850, "06700", "Seco"),
        (-23.480, -46.620, "02040", "Refrigerado"),
        (-23.560, -46.480, "03970", "Seco"),
    ]
    viagem = [
        # (lat, lng, cep, cidade) -- cidades reconhecidas como região
        # externa em regioes_dia_fixo.py::REGIOES
        (-23.501, -47.457, "18040", "Sorocaba"),
        (-22.906, -47.062, "13010", "Campinas"),
    ]

    servicos: list[dict] = []
    sid = 900001

    def _novo(lat, lng, cep, cidade, tipo_carga, nivel=1, caixas=1, bairro="", endereco_custom=None):
        nonlocal sid
        endereco = endereco_custom or (
            f"[TESTE] Rua Fictícia {sid}, {bairro or cidade}, {cidade} - SP, {cep}-000, Brasil"
        )
        if lat is not None:
            _cache_coordenadas[(endereco, gmaps_key or "")] = (lat, lng)
        servicos.append({
            "id": sid, "code": f"TESTE-{sid - 900000:03d}",
            "title": "[TESTE] Entrega fictícia de laboratório",
            "address": endereco, "sender_id": None, "customer": {"code": ""},
            "latitude": lat, "longitude": lng,
            "dimension_3": caixas, "_nivel_dificuldade": nivel, "_tipo_carga": tipo_carga,
        })
        sid += 1

    for nome, lat0, lng0, cep0, qtd, tipo_carga, cidade in clusters:
        for i in range(qtd):
            lat = lat0 + (i % 4) * 0.006
            lng = lng0 + (i // 4) * 0.006
            cep = f"{int(cep0) + i:05d}"
            _novo(lat, lng, cep, cidade, tipo_carga, bairro=nome)

    for lat, lng, cep, tipo_carga in espalhados:
        _novo(lat, lng, cep, "Sao Paulo", tipo_carga)

    for lat, lng, cep, cidade in viagem:
        _novo(lat, lng, cep, cidade, "Seco")

    _novo(None, None, "", "Sao Paulo", "Seco", endereco_custom=
         "[TESTE] Endereço não geocodificável mas com CEP, Sao Paulo - SP, 02199-000, Brasil")
    _novo(None, None, "", "Sao Paulo", "Seco", endereco_custom=
         "[TESTE] Endereço totalmente sem CEP nem coordenada, Sao Paulo - SP, Brasil")

    # Casos de trava de negócio embutidos nos clusters já gerados acima
    # (índices fixos, na ordem em que os clusters foram montados: Casa
    # Verde 0-9, Santo Amaro 10-19, Tatuapé 20-27, Pinheiros 28-35).
    servicos[0]["_nivel_dificuldade"] = 4    # Casa Verde #1 -- rota exclusiva
    servicos[10]["_nivel_dificuldade"] = 3   # Santo Amaro #1 -- limita a 4 entregas/rota
    servicos[11]["_nivel_dificuldade"] = 3   # Santo Amaro #2
    servicos[28]["dimension_3"] = 150        # Pinheiros #1 -- "pedido gigante", rota exclusiva (e também
                                              # já classifica sozinho como veículo VAN/HR, ver abaixo)

    # Grupo de veículo grande (pedido do Hugo, 15/08 -- ver
    # regras/tipo_veiculo.py): 4 endereços diferentes e próximos, cada
    # um com um pedido moderado (nenhum "gigante" sozinho -- todos bem
    # abaixo de VOLUME_MAXIMO_ROTA) que somados já cruzam o piso de 150
    # caixas da menor categoria (VAN/HR) -- testa se separar_pedidos_
    # exclusivos::_extrair_grupos_veiculo_grande consolida os 4 numa
    # rota exclusiva só (170 caixas, 4 endereços), em vez de espalhar
    # entre rotas comuns de última milha.
    for i, cx in enumerate((50, 45, 40, 35)):
        _novo(-23.610 + i * 0.01, -46.480 + i * 0.01, f"{4200 + i:05d}", "Sao Paulo", "Seco",
             caixas=cx, bairro="Vila Prudente")

    return servicos


def _sublote_para_mapa(sublote: list[dict], remetentes_por_id: dict[int, str]) -> list[dict]:
    """Mesma forma de mapa_rotas.py::buscar_rotas_para_mapa -- uma
    parada por pedido com coordenada; pedido sem coordenada não aparece
    no mapa (mas continua contado nas métricas)."""
    paradas = []
    for s in sublote:
        lat, lng = s.get("latitude"), s.get("longitude")
        if not lat or not lng:
            continue
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError):
            continue
        remetente = remetentes_por_id.get(s.get("sender_id"), "Remetente não identificado")
        paradas.append({
            "lat": lat_f, "lng": lng_f,
            "nome": s.get("code", ""), "endereco": s.get("address", ""),
            "titulo": s.get("title", ""), "remetente": remetente,
        })
    return paradas


def buscar_dados_laboratorio(data_alvo: date, particao: str = "Seco", usar_teste: bool = False) -> dict:
    """
    Roda os 5 esquemas de agrupamento sobre os pedidos elegíveis pra
    `data_alvo`, na partição escolhida ("Seco" ou "Refrigerado/
    Congelado" -- nunca misturadas, mesma regra de produção), e devolve:

    {"data_alvo", "particao", "teste", "total_pedidos", "base",
     "google_maps_key",
     "esquemas": {nome: {"rotas":[{"id","nome","paradas":[...]}],
                          "metricas":{"rotas","entregas","caixas",
                                      "km_total","km_medio"}}},
     "esquemas_com_falha": [nomes que não sobreviveram à validação]}

    `usar_teste=True` (pedido do Hugo, 14/08): usa a massa fictícia de
    _gerar_massa_de_teste() em vez de consultar a VUUPT -- não faz
    NENHUMA chamada de rede (nem VUUPT nem geocodificação), pra testar
    o laboratório sem risco de dado fictício ser varrido pelos jobs
    automáticos de produção.
    """
    if particao not in PARTICOES_VALIDAS:
        raise ValueError(f"Partição inválida: {particao!r} (esperado {PARTICOES_VALIDAS})")

    config = _carregar_config()
    gmaps_key = config.get("google_maps", {}).get("api_key", "")

    # Import tardio: só pelas constantes de produção (18 paradas/100
    # caixas/20km/base etc.) -- garante que o laboratório NUNCA destoa
    # dos limites reais sem duplicar os valores aqui, mesmo padrão já
    # usado em planejamento_rotas.py::roteirizar_selecionados.
    import criar_rotas_diarias as crd

    if usar_teste:
        servicos = _gerar_massa_de_teste(gmaps_key)
    else:
        token = config.get("vuupt_api", {}).get("token", "")
        vuupt = VuuptClient(token)
        filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
        servicos_brutos = vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
        servicos = [s for s in servicos_brutos if elegivel_para_data(s, data_alvo)]
        _injetar_classificacoes(servicos, config, crd.DB_PATH)

    servicos_particao = [
        s for s in servicos
        if (s["_tipo_carga"] in TIPOS_CARGA_FRIA) == (particao == "Refrigerado/Congelado")
    ]

    resultado = {
        "data_alvo": data_alvo.isoformat(),
        "particao": particao,
        "teste": usar_teste,
        "total_pedidos": len(servicos_particao),
        "base": None,
        "google_maps_key": gmaps_key,
        "esquemas": {},
        "esquemas_com_falha": [],
    }

    if not servicos_particao:
        return resultado

    coords_base = geocodificar(crd.ENDERECO_BASE, gmaps_key)
    if not coords_base:
        raise RuntimeError("Não consegui geocodificar a base -- laboratório precisa da coordenada da base.")
    base_lat, base_lng = coords_base
    resultado["base"] = {"lat": base_lat, "lng": base_lng}

    eh_viagem_fn = lambda sub: classificar_rota_viagem(sub, gmaps_key)
    particoes_macro = particionar_por_macro_regiao(servicos_particao, gmaps_key)

    def _por_macro(agrupar_uma_particao):
        return [sub for svcs in particoes_macro.values() for sub in agrupar_uma_particao(svcs)]

    candidatos = {
        "Atual (Grade+Greedy)": lambda: _por_macro(lambda svcs: agrupar_atual(
            svcs, gmaps_key, crd.TAMANHO_MINIMO_ROTA, crd.TAMANHO_MAXIMO_ROTA,
            crd.VOLUME_MAXIMO_ROTA, crd.DISTANCIA_MAXIMA_ROTA_KM, crd.DISTANCIA_MAXIMA_VIAGEM_KM)),
        "Sweep Polar": lambda: _por_macro(lambda svcs: agrupar_por_sweep(
            svcs, base_lat, base_lng, tamanho_maximo=crd.TAMANHO_MAXIMO_ROTA,
            volume_maximo=crd.VOLUME_MAXIMO_ROTA, distancia_maxima_km=crd.DISTANCIA_MAXIMA_ROTA_KM,
            api_key=gmaps_key, distancia_maxima_viagem_km=crd.DISTANCIA_MAXIMA_VIAGEM_KM,
            eh_viagem_fn=eh_viagem_fn)),
        "Clarke-Wright": lambda: _por_macro(lambda svcs: agrupar_por_savings(
            svcs, base_lat, base_lng, tamanho_maximo=crd.TAMANHO_MAXIMO_ROTA,
            volume_maximo=crd.VOLUME_MAXIMO_ROTA, distancia_maxima_km=crd.DISTANCIA_MAXIMA_ROTA_KM,
            api_key=gmaps_key, distancia_maxima_viagem_km=crd.DISTANCIA_MAXIMA_VIAGEM_KM,
            eh_viagem_fn=eh_viagem_fn)),
        "CEP real": lambda: _por_macro(lambda svcs: agrupar_por_cep(
            svcs, base_lat, base_lng, tamanho_maximo=crd.TAMANHO_MAXIMO_ROTA,
            volume_maximo=crd.VOLUME_MAXIMO_ROTA, distancia_maxima_km=crd.DISTANCIA_MAXIMA_ROTA_KM,
            api_key=gmaps_key, distancia_maxima_viagem_km=crd.DISTANCIA_MAXIMA_VIAGEM_KM,
            eh_viagem_fn=eh_viagem_fn)),
        "K-means geográfico": lambda: _por_macro(lambda svcs: agrupar_por_kmeans(
            svcs, base_lat, base_lng, tamanho_maximo=crd.TAMANHO_MAXIMO_ROTA,
            volume_maximo=crd.VOLUME_MAXIMO_ROTA, distancia_maxima_km=crd.DISTANCIA_MAXIMA_ROTA_KM,
            api_key=gmaps_key, distancia_maxima_viagem_km=crd.DISTANCIA_MAXIMA_VIAGEM_KM,
            eh_viagem_fn=eh_viagem_fn)),
    }

    avaliacoes = avaliar_candidatos(
        servicos_particao, candidatos, base_lat, base_lng, gmaps_key,
        crd.TAMANHO_MAXIMO_ROTA, crd.VOLUME_MAXIMO_ROTA,
        label=f"{particao} (teste)" if usar_teste else particao,
    )

    resultado["esquemas_com_falha"] = [nome for nome in candidatos if nome not in avaliacoes]

    remetentes_por_id = carregar_remetentes_por_sender_id()
    for nome, avaliacao in avaliacoes.items():
        rotas_mapa = []
        for i, sublote in enumerate(avaliacao["sublotes"]):
            paradas = _sublote_para_mapa(sublote, remetentes_por_id)
            if not paradas:
                continue
            tipo_veiculo = classificar_tipo_veiculo(*caixas_e_enderecos(sublote))
            nome_rota = f"Rota {i + 1} ({len(sublote)} entregas)"
            if tipo_veiculo:
                nome_rota += f" [veículo: {tipo_veiculo.nome}]"
            rotas_mapa.append({
                "id": f"{nome}__{i}",
                "nome": nome_rota,
                "tipo_veiculo": tipo_veiculo.codigo if tipo_veiculo else None,
                "paradas": paradas,
            })
        resultado["esquemas"][nome] = {
            "rotas": rotas_mapa,
            "metricas": {
                "rotas": avaliacao["rotas"],
                "entregas": avaliacao["entregas"],
                "caixas": avaliacao["caixas"],
                "km_total": round(avaliacao["km_total"], 1),
                "km_medio": round(avaliacao["km_medio"], 1),
            },
        }

    return resultado
