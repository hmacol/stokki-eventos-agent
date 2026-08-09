# -*- coding: utf-8 -*-
"""
roteirizacao_dados.py

Seleção dos pedidos corretos pra cada planejamento de rota (pedido do
Hugo, 31/07-01/08): busca os serviços 'not_assigned' no VUUPT, agrupa
por REGIÃO geográfica, funde regiões pequenas até um MÍNIMO de 10
pedidos, e divide regiões grandes em sublotes balanceados de até 15 —
pra rodar 1 planejamento de rota por sublote, sempre com um volume que
faça sentido (nem pouco demais, nem além da capacidade do veículo).

Coordenadas reais (01/08, pedido do Hugo: "usar a geocodificação já
gerada no outro agente"): reaproveita geocodificacao.py (o MESMO
módulo que o pipeline de importação usa, com cache em dados/dados.db,
tabela geocache) — como praticamente todo pedido not_assigned já foi
geocodificado na importação, consultar aqui normalmente é um cache
hit, sem gastar chamada nova no Google Maps. Onde não tiver coordenada
disponível, cai pro CEP como reserva -- nunca quebra por falta de
coordenada.

Reaproveita vuupt_client.py (mesmo domínio app.vuupt.com de sempre)
pra listar os serviços — a etapa de OTIMIZAÇÃO em si usa um domínio e
endpoint diferentes (api.vuupt.com/route-optimization), ver
otimizacao_client.py.
"""
import logging
import math
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from geocodificacao import geocodificar

logger = logging.getLogger(__name__)


def elegivel_para_data(servico: dict, data_alvo) -> bool:
    """
    Filtro de agendamento (pedido do Hugo, 02/08: "pedidos com
    agendamento só deveriam entrar em rota nas datas corretas, já
    agendadas"). `data_alvo` é um objeto date (o dia sendo roteirizado
    -- normalmente "amanhã").

    - Pedido SEM agendamento (campo scheduled_start vazio/ausente):
      elegível pra qualquer data.
    - Pedido COM agendamento: elegível se a data de scheduled_start for
      HOJE (data_alvo) OU ANTERIOR (agendamento vencido/atrasado --
      melhor rotear atrasado do que nunca) -- só fica de fora quando a
      data agendada é FUTURA em relação a data_alvo (precisa esperar o
      dia certo; reentra automaticamente quando esse dia for roteirizado).

    scheduled_start vem em ISO8601 com offset, ex: "2026-08-03T09:00:00-03:00".
    Se o formato vier inesperado (não ISO8601 parseável), o pedido é
    tratado como SEM agendamento (mais seguro deixar entrar do que
    travar o pipeline por causa de 1 campo malformado).
    """
    scheduled_start = servico.get("scheduled_start")
    if not scheduled_start:
        return True
    try:
        data_agendada = datetime.fromisoformat(scheduled_start).date()
    except (ValueError, TypeError):
        return True
    return data_agendada <= data_alvo


def extrair_cep(servico: dict) -> str | None:
    """
    Extrai o CEP (8 dígitos, sem pontuação) do campo 'address' do
    serviço VUUPT. Dois formatos reais observados (confirmado com dado
    de produção, 31/07):
      - Predominante: "..., Cidade - UF, 12345-678, Brasil" (CEP com
        hífen, geralmente seguido de ", Brasil")
      - Raro: "..., Cidade - UF, 12345678" (sem hífen, sem ", Brasil"
        -- visto em pelo menos 1 pedido com endereço aparentemente
        malformado/distante -- ver #PS-32887)
    Retorna None se não achar um CEP plausível em nenhum dos formatos.
    """
    endereco = servico.get("address") or ""
    match = re.search(r"(\d{5})-(\d{3})", endereco)
    if match:
        return match.group(1) + match.group(2)
    match = re.search(r"(\d{8})\s*$", endereco.strip())
    if match:
        return match.group(1)
    return None


def obter_coordenadas(servico: dict, api_key: str | None) -> tuple[float, float] | None:
    """
    Busca as coordenadas do endereço do serviço, reaproveitando o
    MESMO cache de geocodificação usado pelo pipeline de importação
    (geocodificacao.py, tabela geocache em dados/dados.db) -- a imensa
    maioria dos pedidos not_assigned já foi geocodificada por lá, isso
    normalmente é um cache hit, sem chamada nova no Google Maps.
    Retorna None se não houver endereço, chave de API, ou coordenada
    (cache miss + falha do Google, ou endereço não resolvido antes).
    """
    endereco = servico.get("address")
    if not endereco:
        return None
    return geocodificar(endereco, api_key or "")


def _distancia_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distância aproximada em km entre duas coordenadas (haversine)."""
    R = 6371.0
    lat1r, lng1r, lat2r, lng2r = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2r - lat1r
    dlng = lng2r - lng1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def agrupar_por_regiao(servicos: list[dict], api_key: str | None = None,
                       digitos_prefixo: int = 2, tamanho_grade_graus: float = 0.1) -> dict[str, list[dict]]:
    """
    Agrupa os serviços por região geográfica. Quando há coordenada
    disponível (cache de geocodificação, ver obter_coordenadas),
    agrupa por CÉLULA DE GRADE (lat/lng arredondados pra
    tamanho_grade_graus, ~11km por padrão) -- proximidade real, não
    proxy. Sem coordenada, cai pro prefixo do CEP (2 dígitos por
    padrão) como reserva; sem CEP nenhum, vai pro grupo "sem_localizacao".

    Este agrupamento inicial é intencionalmente fino (regiões pequenas)
    -- quem chama deve usar consolidar_regioes_pequenas() em seguida
    pra fundir até o volume mínimo desejado por rota.
    """
    grupos: dict[str, list[dict]] = defaultdict(list)
    for s in servicos:
        coords = obter_coordenadas(s, api_key)
        if coords:
            lat, lng = coords
            lat_grade = round(lat / tamanho_grade_graus) * tamanho_grade_graus
            lng_grade = round(lng / tamanho_grade_graus) * tamanho_grade_graus
            regiao = f"{lat_grade:.2f},{lng_grade:.2f}"
        else:
            cep = extrair_cep(s)
            regiao = cep[:digitos_prefixo] if cep else "sem_localizacao"
        grupos[regiao].append(s)
    return dict(grupos)


def _centroide_coords(servicos: list[dict], api_key: str | None) -> tuple[float, float] | None:
    coords_lista = [c for c in (obter_coordenadas(s, api_key) for s in servicos) if c]
    if not coords_lista:
        return None
    return (
        sum(c[0] for c in coords_lista) / len(coords_lista),
        sum(c[1] for c in coords_lista) / len(coords_lista),
    )


def _centroide_cep(servicos: list[dict]) -> float | None:
    ceps = [int(extrair_cep(s)) for s in servicos if extrair_cep(s)]
    if not ceps:
        return None
    return sum(ceps) / len(ceps)


def consolidar_regioes_pequenas(grupos: dict[str, list[dict]], minimo: int = 10,
                                api_key: str | None = None) -> dict[str, list[dict]]:
    """
    Funde regiões pequenas (menos de `minimo` pedidos — pedido do
    Hugo, 01/08: "mínimo de 10 pedidos em cada rota") com a região
    vizinha mais PRÓXIMA, repetidamente, até que todas as regiões
    resultantes tenham pelo menos `minimo` pedidos -- ou até restar
    uma única região (não dá pra forçar um mínimo que o total de
    pedidos disponíveis, sozinho, não alcança; nesse caso a região
    única fica menor que o mínimo mesmo, é o melhor possível).

    Também cobre o caso de regiões com só 1 pedido (a API de
    otimização exige mínimo de 2 atividades) -- com minimo>=2 (padrão
    é 10), isso sempre é resolvido no caminho.

    Proximidade pelo CENTROIDE de coordenadas reais quando disponível
    (mesma lógica geográfica do resto do módulo); cai pro CEP médio
    como reserva. Nunca deixa nenhum pedido de fora.
    """
    atual = {regiao: list(servicos) for regiao, servicos in grupos.items()}

    while len(atual) > 1:
        pequenas = [r for r, s in atual.items() if len(s) < minimo]
        if not pequenas:
            break

        menor = min(pequenas, key=lambda r: len(atual[r]))
        candidatas = [r for r in atual if r != menor]

        centroide_menor = _centroide_coords(atual[menor], api_key)
        candidatas_com_coords = [c for c in candidatas if _centroide_coords(atual[c], api_key)]
        if centroide_menor and candidatas_com_coords:
            vizinha = min(
                candidatas_com_coords,
                key=lambda r: _distancia_km(*centroide_menor, *_centroide_coords(atual[r], api_key)),
            )
        else:
            cep_menor = _centroide_cep(atual[menor])
            candidatas_com_cep = [c for c in candidatas if _centroide_cep(atual[c]) is not None]
            if cep_menor is not None and candidatas_com_cep:
                vizinha = min(candidatas_com_cep, key=lambda r: abs(cep_menor - _centroide_cep(atual[r])))
            else:
                vizinha = max(candidatas, key=lambda r: len(atual[r]))  # último recurso

        logger.info(
            f"  Consolidando: região '{menor}' ({len(atual[menor])} pedido(s)) "
            f"fundida em '{vizinha}' ({len(atual[vizinha])} pedido(s))."
        )
        atual[vizinha].extend(atual[menor])
        del atual[menor]

    return atual


def _tamanhos_balanceados(total: int, minimo: int, maximo: int) -> list[int]:
    """
    Divide `total` pedidos em grupos o mais equilibrados possível,
    cada um dentro de [minimo, maximo] quando matematicamente
    possível. Ex: 27 com min=10/max=15 -> [14,13] (não [15,12]).

    Quando total não permite respeitar os dois limites ao mesmo tempo
    (ex: 17 só divide em 2 grupos de 8-9, abaixo do mínimo de 10),
    prioriza o MÁXIMO (limite físico do veículo) e avisa no log.
    """
    if total <= maximo:
        return [total]
    k = math.ceil(total / maximo)
    base = total // k
    resto = total % k
    tamanhos = [base + 1 if i < resto else base for i in range(k)]
    if min(tamanhos) < minimo:
        logger.warning(
            f"{total} pedido(s) não dividem em grupos de pelo menos {minimo} respeitando "
            f"o máximo de {maximo} -- menor grupo ficará com {min(tamanhos)} (melhor possível)."
        )
    return tamanhos


def dividir_em_sublotes(servicos: list[dict], tamanho_minimo: int = 10, tamanho_maximo: int = 15,
                        api_key: str | None = None) -> list[list[dict]]:
    """
    Divide uma região grande em sublotes BALANCEADOS entre
    `tamanho_minimo` e `tamanho_maximo` pedidos cada (pedido do Hugo,
    01/08: "mínimo de 10... forçar os endereços mais próximos" — e
    antes, 01/08: "no máximo 15, mas não necessariamente 15" — o
    veículo TAPIOCA aguenta até 20, o Hugo quer uma margem).

    Considera PROXIMIDADE real: ordena os serviços por coordenada
    (lat, lng) quando disponível antes de dividir, pra que cada
    sublote fique com pedidos geograficamente vizinhos entre si; cai
    pra CEP completo como reserva quando não há coordenada.

    Se a região já tem <= tamanho_maximo, retorna ela inteira como um
    único sublote (sem dividir à toa, mesmo que fique abaixo do
    mínimo — nesse caso quem chama decide, ver consolidar_regioes_
    pequenas, que já devia ter garantido o mínimo antes de chegar aqui).
    """
    if len(servicos) <= tamanho_maximo:
        return [list(servicos)]

    def _chave_ordenacao(servico: dict):
        coords = obter_coordenadas(servico, api_key)
        if coords:
            return (0, coords[0], coords[1])
        cep = extrair_cep(servico)
        return (1, int(cep) if cep else float("inf"), 0.0)

    ordenados = sorted(servicos, key=_chave_ordenacao)
    tamanhos = _tamanhos_balanceados(len(servicos), tamanho_minimo, tamanho_maximo)

    sublotes = []
    inicio = 0
    for tamanho in tamanhos:
        sublotes.append(ordenados[inicio:inicio + tamanho])
        inicio += tamanho
    return sublotes


def ordenar_por_distancia_base(servicos: list[dict], base_lat: float, base_lng: float,
                               api_key: str | None = None) -> list[dict]:
    """
    Ordena os serviços de uma rota da mais LONGE pra mais PERTO da
    base -- padrão de sequenciamento pedido pelo Hugo, 03/08 (rotas
    sempre saem da base indo primeiro pro ponto mais distante,
    "esvaziando" o caminho de volta).

    Usa a coordenada já embutida no próprio serviço (latitude/
    longitude, quando o serviço já veio de uma rota existente via
    include=services) quando disponível -- mais rápido, sem gastar
    geocodificação de novo; cai pra obter_coordenadas() como reserva.
    Serviço sem nenhuma coordenada disponível vai pro FINAL da lista
    (não quebra, só fica na posição menos previsível).
    """
    def _distancia_do_servico(servico: dict) -> float:
        lat, lng = servico.get("latitude"), servico.get("longitude")
        if lat and lng:
            try:
                return _distancia_km(float(lat), float(lng), base_lat, base_lng)
            except (TypeError, ValueError):
                pass
        coords = obter_coordenadas(servico, api_key)
        if coords:
            return _distancia_km(coords[0], coords[1], base_lat, base_lng)
        return -1.0  # sem coordenada -- fica no final da ordem decrescente

    return sorted(servicos, key=_distancia_do_servico, reverse=True)
