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
from regioes_dia_fixo import RAIO_GRANDE_SP_KM, extrair_cidade, regiao_externa_da_cidade

logger = logging.getLogger(__name__)

# Coordenada de referência do centro de São Paulo (Praça da Sé) -- a
# mesma já usada em alocacao_motoristas.py (COORD_BASE_SP) e zonas_sp.py
# (COORD_CENTRO_SP) como origem do raio da Grande SP.
COORD_CENTRO_SP = (-23.550520, -46.633309)

# Macro-regiões (pedido do Hugo, 12/08: "limitador dentro da Grande São
# Paulo... pedidos de Sorocaba não se misturariam com pedidos de Barueri
# automaticamente"): TRAVA RÍGIDA de partição -- nenhuma rota mistura
# pedidos de macro-regiões diferentes. As macros são: GRANDE_SP (dentro
# do raio de 70km e fora de região externa), o NOME de cada região
# externa de dia fixo (Sorocaba, Campinas, Vale do Paraíba, Baixada
# Santista, Piracicaba -- cada uma é uma direção/estrada diferente,
# também não se misturam ENTRE SI), e VIAGEM pra pedido a mais de 70km
# sem região externa cadastrada.
MACRO_GRANDE_SP = "GRANDE_SP"
MACRO_VIAGEM_GENERICA = "VIAGEM"


def macro_regiao_do_servico(servico: dict, api_key: str | None = None) -> str:
    """
    Macro-região de UM serviço: nome da região externa (pela cidade do
    endereço), VIAGEM se a coordenada está a mais de RAIO_GRANDE_SP_KM
    do centro de SP sem região externa cadastrada, senão GRANDE_SP.
    Sem cidade E sem coordenada reconhecível: GRANDE_SP (mesmo padrão
    seguro do resto do módulo -- dado ausente não muda comportamento,
    o pedido segue no fluxo urbano normal).
    """
    cidade = extrair_cidade(servico)
    if cidade:
        externa = regiao_externa_da_cidade(cidade)
        if externa:
            return externa

    coords = obter_coordenadas(servico, api_key)
    if coords and _distancia_km(*coords, *COORD_CENTRO_SP) > RAIO_GRANDE_SP_KM:
        return MACRO_VIAGEM_GENERICA
    return MACRO_GRANDE_SP


def particionar_por_macro_regiao(servicos: list[dict], api_key: str | None = None) -> dict[str, list[dict]]:
    """Particiona os serviços por macro-região (ver macro_regiao_do_servico)
    -- quem roteiriza deve rodar o agrupamento SEPARADO por partição, pra
    nenhuma rota cruzar a fronteira Grande SP x regiões externas."""
    particoes: dict[str, list[dict]] = defaultdict(list)
    for s in servicos:
        particoes[macro_regiao_do_servico(s, api_key)].append(s)
    return dict(particoes)


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


def extrair_volume_caixas(servico: dict) -> int:
    """
    Quantidade de caixas/volumes do pedido, vinda do campo
    'dimension_3' do serviço VUUPT (pedido do Hugo, 09/08: limite de
    100 caixas por rota, além do limite de entregas). Fallback seguro
    pra 1 caixa quando o campo vem ausente/nulo/não numérico -- nunca
    quebra o agrupamento por causa de 1 pedido sem essa dimensão
    preenchida.
    """
    vol = servico.get("dimension_3")
    if vol is not None:
        try:
            return max(1, int(vol))
        except (ValueError, TypeError):
            pass
    return 1


def extrair_nivel_dificuldade(servico: dict) -> int:
    """
    Nível de dificuldade de entrega (1 a 4) do pedido -- não vem nativo
    do VUUPT, é injetado no dict do serviço em criar_rotas_diarias.py
    (chave '_nivel_dificuldade', via regras.complexidade_entrega) ANTES
    de chamar as funções deste módulo. Sem essa chave (ex: chamado fora
    desse fluxo), assume o nível mais leniente (1) -- nunca quebra o
    agrupamento por falta dela.
    """
    return servico.get("_nivel_dificuldade") or 1


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


# Memoização em memória por execução -- obter_coordenadas() é chamada
# várias vezes pro MESMO serviço em estágios diferentes do agrupamento
# (agrupar_por_regiao, dividir_em_sublotes, ordenar_por_distancia_base,
# e repetidamente dentro do laço de merge de consolidar_regioes_pequenas).
# geocodificar() já cacheia em SQLite entre execuções, mas cada chamada
# ainda abre uma conexão nova -- pra uma rodada com algumas centenas de
# pedidos isso vira milhares de conexões redundantes por execução. Esse
# cache evita reconsultar o mesmo endereço mais de uma vez por rodada.
_cache_coordenadas: dict[tuple[str, str], tuple[float, float] | None] = {}


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
    chave = (endereco, api_key or "")
    if chave in _cache_coordenadas:
        return _cache_coordenadas[chave]
    resultado = geocodificar(endereco, api_key or "")
    _cache_coordenadas[chave] = resultado
    return resultado


def _distancia_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Distância aproximada em km entre duas coordenadas (haversine)."""
    R = 6371.0
    lat1r, lng1r, lat2r, lng2r = map(math.radians, [lat1, lng1, lat2, lng2])
    dlat = lat2r - lat1r
    dlng = lng2r - lng1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def calcular_km_estimado(sublote: list[dict], base_lat: float, base_lng: float,
                         api_key: str | None) -> float:
    """
    KM total estimado (haversine) de UMA rota, na ordem de visita:
    base -> p1 -> ... -> pN -> base. Serviço sem coordenada é ignorado
    no somatório (não dá pra medir). Extraída de selecao_modelo.py::
    _km_total (que soma isso sobre vários sublotes) pra ser reaproveitada
    também pelo cálculo de km dos rascunhos de rota (painel_agentes/
    rascunhos_rota.py), evitando duas implementações divergindo.
    """
    coords = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
    if not coords:
        return 0.0
    total = _distancia_km(base_lat, base_lng, *coords[0])
    for i in range(len(coords) - 1):
        total += _distancia_km(*coords[i], *coords[i + 1])
    total += _distancia_km(*coords[-1], base_lat, base_lng)
    return total


def agrupar_por_regiao(servicos: list[dict], api_key: str | None = None,
                       digitos_prefixo: int = 2, tamanho_grade_graus: float = 0.1) -> dict[str, list[dict]]:
    """
    Agrupa os serviços por região geográfica. Quando há coordenada
    disponível (cache de geocodificação, ver obter_coordenadas),
    agrupa por CÉLULA DE GRADE (lat/lng arredondados pra
    tamanho_grade_graus, ~11km por padrão) -- proximidade real, não
    proxy. Sem coordenada, cai pro prefixo do CEP (2 dígitos por
    padrão) como reserva; sem CEP nenhum, vai pro grupo "sem_localizacao".

    A chave de cada região vem PREFIXADA com a macro-região
    ("GRANDE_SP|-23.55,-46.63", "Sorocaba|-23.47,-47.45"...) -- pedido
    do Hugo, 12/08: consolidar_regioes_pequenas só funde regiões da
    MESMA macro, então nenhuma rota mistura Grande SP com região
    externa, nem duas regiões externas entre si.

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
        macro = macro_regiao_do_servico(s, api_key)
        grupos[f"{macro}|{regiao}"].append(s)
    return dict(grupos)


def _macro_da_chave(chave_regiao: str) -> str:
    """Macro-região embutida na chave de agrupar_por_regiao ("Macro|célula").
    Chave sem prefixo (chamador antigo/externo): macro vazia -- todas as
    regiões sem prefixo continuam podendo se fundir entre si, como antes."""
    return chave_regiao.split("|", 1)[0] if "|" in chave_regiao else ""


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

    Só funde regiões da MESMA macro-região (prefixo da chave, ver
    agrupar_por_regiao -- pedido do Hugo, 12/08): região pequena sem
    vizinha na própria macro fica pequena mesmo (uma rota de Sorocaba
    com 3 pedidos é melhor que Sorocaba fundida com Barueri).

    Proximidade pelo CENTROIDE de coordenadas reais quando disponível
    (mesma lógica geográfica do resto do módulo); cai pro CEP médio
    como reserva. Nunca deixa nenhum pedido de fora.
    """
    atual = {regiao: list(servicos) for regiao, servicos in grupos.items()}
    sem_vizinha: set[str] = set()  # pequenas sem candidata na própria macro -- não readressáveis

    while len(atual) > 1:
        pequenas = [r for r, s in atual.items() if len(s) < minimo and r not in sem_vizinha]
        if not pequenas:
            break

        menor = min(pequenas, key=lambda r: len(atual[r]))
        candidatas = [r for r in atual if r != menor and _macro_da_chave(r) == _macro_da_chave(menor)]
        if not candidatas:
            sem_vizinha.add(menor)
            continue

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


# Nível de dificuldade 3: pode misturar com níveis 1/2, mas a rota
# inteira fica limitada a este tamanho assim que QUALQUER entrega
# nível 3 entra nela (pedido do Hugo, 10/08). Nível 4 é mais estrito
# ainda: nunca divide rota com nenhum outro pedido (ver NIVEL_ROTA_EXCLUSIVA).
NIVEL_3_TAMANHO_MAXIMO_ROTA = 4
NIVEL_ROTA_EXCLUSIVA = 4


def dividir_em_sublotes(servicos: list[dict], tamanho_minimo: int = 10, tamanho_maximo: int = 18,
                        volume_maximo: int = 100, distancia_maxima_km: float | None = 15,
                        api_key: str | None = None) -> list[list[dict]]:
    """
    Divide uma região grande em sublotes respeitando QUATRO travas ao
    mesmo tempo (pedido do Hugo, 09/08: "no máximo 18 entregas OU 100
    caixas por rota, o que vier primeiro" -- e depois, 09/08: "máximo
    de 15km de distância entre pedidos da mesma rota", achado ao
    revisar a rota #6 do dia, que tinha pego um pedido de Niterói-RJ
    junto com pedidos de São Paulo por ser "a rota mais próxima com
    espaço", mesmo estando a mais de 300km; e 10/08: nível de
    dificuldade da entrega, ver extrair_nivel_dificuldade):
      - até `tamanho_maximo` entregas por sublote (18 por padrão) --
        reduzido para NIVEL_3_TAMANHO_MAXIMO_ROTA (4) assim que o
        sublote contém alguma entrega nível 3; entrega nível 4 nunca
        divide sublote com mais ninguém (rota exclusiva, mesmo
        tratamento do pedido "gigante" de caixas, abaixo);
      - até `volume_maximo` caixas (soma de extrair_volume_caixas) por
        sublote;
      - nenhum par de pedidos do MESMO sublote pode estar a mais de
        `distancia_maxima_km` um do outro (quando ambos têm
        coordenada -- sem coordenada não dá pra checar, não bloqueia).
        `distancia_maxima_km=None` desliga essa trava por completo
        (pedido do Hugo, 10/08: rotas de Viagem não têm limite de
        distância -- só as outras travas de tamanho/volume/nível valem).

    Considera PROXIMIDADE real: ordena os serviços por coordenada
    (lat, lng) quando disponível antes de dividir, pra que cada
    sublote fique com pedidos geograficamente vizinhos entre si; cai
    pra CEP completo como reserva quando não há coordenada. Sobre essa
    ordem já próxima, empacota de forma GANANCIOSA (greedy bin
    packing): vai enchendo o sublote atual até que o próximo pedido
    estoure uma das travas, aí fecha o sublote e abre outro -- isso
    tende a aproximar cada rota do limite (18 ou 100), sem nunca
    estourar nenhuma das travas.

    Exceção de pedido gigante ou nível 4: um único pedido com mais de
    `volume_maximo` caixas, ou com nível de dificuldade 4, nunca cabe
    junto com nenhum outro -- aloca uma rota exclusiva isolada só pra ele.
    """
    def _chave_ordenacao(servico: dict):
        coords = obter_coordenadas(servico, api_key)
        if coords:
            return (0, coords[0], coords[1])
        cep = extrair_cep(servico)
        return (1, int(cep) if cep else float("inf"), 0.0)

    def _cabe_na_distancia(servico: dict, sublote_atual: list[dict]) -> bool:
        if distancia_maxima_km is None:
            return True
        coords_novo = obter_coordenadas(servico, api_key)
        if not coords_novo:
            return True
        for outro in sublote_atual:
            coords_outro = obter_coordenadas(outro, api_key)
            if coords_outro and _distancia_km(*coords_novo, *coords_outro) > distancia_maxima_km:
                return False
        return True

    ordenados = sorted(servicos, key=_chave_ordenacao)

    sublotes: list[list[dict]] = []
    sublote_atual: list[dict] = []
    caixas_atual = 0

    for servico in ordenados:
        cx_pedido = extrair_volume_caixas(servico)
        nivel_pedido = extrair_nivel_dificuldade(servico)

        if cx_pedido > volume_maximo or nivel_pedido == NIVEL_ROTA_EXCLUSIVA:
            if sublote_atual:
                sublotes.append(sublote_atual)
                sublote_atual = []
                caixas_atual = 0
            sublotes.append([servico])
            continue

        tem_nivel_3 = nivel_pedido == 3 or any(extrair_nivel_dificuldade(s) == 3 for s in sublote_atual)
        tamanho_maximo_efetivo = NIVEL_3_TAMANHO_MAXIMO_ROTA if tem_nivel_3 else tamanho_maximo

        cabe_entregas = len(sublote_atual) + 1 <= tamanho_maximo_efetivo
        cabe_caixas = caixas_atual + cx_pedido <= volume_maximo
        cabe_distancia = _cabe_na_distancia(servico, sublote_atual)

        if sublote_atual and not (cabe_entregas and cabe_caixas and cabe_distancia):
            sublotes.append(sublote_atual)
            sublote_atual = []
            caixas_atual = 0

        sublote_atual.append(servico)
        caixas_atual += cx_pedido

    if sublote_atual:
        sublotes.append(sublote_atual)

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
