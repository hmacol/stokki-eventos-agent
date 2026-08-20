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
from regras.tipo_veiculo import classificar_tipo_veiculo, teto_caixas_para_enderecos

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


def particionar_por_macro_regiao(servicos: list[dict], api_key: str | None = None,
                                 tamanho_minimo: int = 1,
                                 distancia_maxima_fusao_km: float | None = None) -> dict[str, list[dict]]:
    """
    Particiona os serviços por macro-região (ver macro_regiao_do_servico)
    -- quem roteiriza deve rodar o agrupamento SEPARADO por partição, pra
    nenhuma rota cruzar a fronteira Grande SP x regiões externas.

    Fusão entre macro-regiões vizinhas, como ÚLTIMO RECURSO (pedido do
    Hugo, 15/08): quando `distancia_maxima_fusao_km` é informado, toda
    macro-região com menos de `tamanho_minimo` pedidos tenta se fundir
    na OUTRA macro-região mais PRÓXIMA (centroide real, mesma lógica de
    consolidar_regioes_pequenas) -- mas só se a distância entre os
    centroides ficar até esse teto; do contrário, permanece isolada
    (mais seguro que forçar uma rota gigante sem sentido). Achado real,
    15/08: Sorocaba e Baixada Santista caem no MESMO dia fixo da semana
    mas são direções opostas (128km entre si) -- corretamente NÃO se
    fundem; Campinas e Piracicaba, mesmo dia fixo E mesmo corredor
    (44km) -- se fundem. Sem esse parâmetro (None, padrão): comportamento
    de sempre, macro-regiões 100% isoladas.
    """
    particoes: dict[str, list[dict]] = defaultdict(list)
    for s in servicos:
        particoes[macro_regiao_do_servico(s, api_key)].append(s)
    particoes = dict(particoes)

    if distancia_maxima_fusao_km is None:
        return particoes

    sem_vizinha: set[str] = set()
    while len(particoes) > 1:
        pequenas = [r for r, s in particoes.items() if len(s) < tamanho_minimo and r not in sem_vizinha]
        if not pequenas:
            break
        menor = min(pequenas, key=lambda r: len(particoes[r]))

        candidatas = [r for r in particoes if r != menor]
        centroide_menor = _centroide_coords(particoes[menor], api_key)
        candidatas_com_coords = [c for c in candidatas if _centroide_coords(particoes[c], api_key)]
        if not centroide_menor or not candidatas_com_coords:
            sem_vizinha.add(menor)
            continue

        vizinha, distancia = min(
            ((c, _distancia_km(*centroide_menor, *_centroide_coords(particoes[c], api_key)))
             for c in candidatas_com_coords),
            key=lambda t: t[1],
        )
        if distancia > distancia_maxima_fusao_km:
            sem_vizinha.add(menor)
            continue

        logger.info(
            f"  Fusão entre macro-regiões: '{menor}' ({len(particoes[menor])} pedido(s)) "
            f"fundida em '{vizinha}' ({len(particoes[vizinha])} pedido(s)) -- {distancia:.1f} km."
        )
        particoes[vizinha].extend(particoes[menor])
        del particoes[menor]

    return particoes


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


def caixas_e_enderecos(sublote: list[dict]) -> tuple[int, int]:
    """
    (soma de caixas, nº de endereços distintos) de um sublote -- usado
    por regras.tipo_veiculo.classificar_tipo_veiculo, tanto no
    empacotamento (separar_pedidos_exclusivos, abaixo) quanto na
    alocação de motorista (alocacao_motoristas.py) e na tag exibida nos
    rascunhos (criar_rotas_diarias.py). Endereço vem do campo 'address'
    do serviço VUUPT (mesmo campo lido por extrair_cep); pedido sem
    endereço reconhecível (None) conta como 1 endereço próprio, nunca é
    descartado da contagem.
    """
    caixas = sum(extrair_volume_caixas(s) for s in sublote)
    enderecos = {s.get("address") for s in sublote}
    return caixas, len(enderecos)


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


def _chave_nivel4(servico: dict) -> tuple[object, object]:
    """Chave de junção de pedidos nível 4 -- ver separar_pedidos_exclusivos:
    (endereço, dia de agendamento). Dois nível 4 só dividem rota com o
    MESMO endereço de entrega; a data de agendamento só entra na
    comparação quando AMBOS têm agendamento (pedido do Hugo, 17/08 --
    substitui a regra anterior de "mesma rede", que juntava endereços
    diferentes por raiz de CNPJ). Sem agendamento (`_dia_agendamento`
    retorna None pros dois) junta só por endereço; um agendado + um sem
    agendamento nunca junta (chaves com `dia` None x data nunca batem),
    mesmo endereço igual."""
    return (servico.get("address"), _dia_agendamento(servico))


def _dia_agendamento(servico: dict):
    """Data (sem hora) do agendamento do serviço (campo scheduled_start),
    ou None se não tiver agendamento ou vier num formato não parseável
    -- mesma tolerância de elegivel_para_data."""
    scheduled_start = servico.get("scheduled_start")
    if not scheduled_start:
        return None
    try:
        return datetime.fromisoformat(scheduled_start).date()
    except (ValueError, TypeError):
        return None


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


def _espalhamento_maximo(servicos: list[dict], api_key: str | None) -> float:
    """Maior distância par a par entre os pedidos com coordenada
    disponível (0.0 se não houver pelo menos 2 com coordenada -- não dá
    pra medir, não bloqueia)."""
    coords = [c for c in (obter_coordenadas(s, api_key) for s in servicos) if c]
    maior = 0.0
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            maior = max(maior, _distancia_km(*coords[i], *coords[j]))
    return maior


def consolidar_regioes_pequenas(grupos: dict[str, list[dict]], minimo: int = 10,
                                api_key: str | None = None,
                                distancia_maxima_km: float | None = None) -> dict[str, list[dict]]:
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

    `distancia_maxima_km` (Hugo, 15/08): quando informado, só aceita
    fundir com uma vizinha se o ESPALHAMENTO resultante (maior par a
    par, ver _espalhamento_maximo) ficar dentro desse teto -- a MESMA
    trava que dividir_em_sublotes vai aplicar depois. Sem isso, a fusão
    podia perseguir a vizinha mais próxima só pra bater o mínimo de
    CONTAGEM mesmo estando longe, e dividir_em_sublotes quebrava esse
    grupo de novo por DISTÂNCIA -- desperdiçando a consolidação e ainda
    deixando sobra pequena (achado real, 15/08: região de 46 pedidos
    com 29km de espalhamento virava [1, 9, 18, 18] em vez de aproveitar
    melhor o volume). Quando a vizinha mais próxima estoura o teto,
    tenta a PRÓXIMA mais próxima -- só marca sem_vizinha quando
    NENHUMA candidata (de qualquer distância) serve. Sem esse parâmetro
    (None, padrão): comportamento de sempre, sempre funde com a mais
    próxima.
    """
    atual = {regiao: list(servicos) for regiao, servicos in grupos.items()}
    sem_vizinha: set[str] = set()  # pequenas sem candidata (na macro, ou dentro do teto) -- não readressáveis

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
            ordenadas = sorted(
                candidatas_com_coords,
                key=lambda r: _distancia_km(*centroide_menor, *_centroide_coords(atual[r], api_key)),
            )
        else:
            cep_menor = _centroide_cep(atual[menor])
            candidatas_com_cep = [c for c in candidatas if _centroide_cep(atual[c]) is not None]
            if cep_menor is not None and candidatas_com_cep:
                ordenadas = sorted(candidatas_com_cep, key=lambda r: abs(cep_menor - _centroide_cep(atual[r])))
            else:
                ordenadas = sorted(candidatas, key=lambda r: -len(atual[r]))  # último recurso

        vizinha = None
        for candidata in ordenadas:
            if distancia_maxima_km is not None:
                espalhamento = _espalhamento_maximo(atual[menor] + atual[candidata], api_key)
                if espalhamento > distancia_maxima_km:
                    continue
            vizinha = candidata
            break

        if vizinha is None:
            sem_vizinha.add(menor)
            continue

        logger.info(
            f"  Consolidando: região '{menor}' ({len(atual[menor])} pedido(s)) "
            f"fundida em '{vizinha}' ({len(atual[vizinha])} pedido(s))."
        )
        atual[vizinha].extend(atual[menor])
        del atual[menor]

    return atual


# Nível de dificuldade 3: pode misturar livremente com níveis 1/2 (que
# PREENCHEM a rota normalmente, até `tamanho_maximo`/`volume_maximo`).
# A quantidade de nível 3 numa mesma rota NÃO tem mais um teto fixo de
# pedidos -- pedido do Hugo, 20/08: em vez disso, cada rota tem um
# ORÇAMENTO DE HORAS (ROTA_TEMPO_MAXIMO_HORAS, 9h); cada nível 3 custa
# TEMPO_NIVEL3_HORAS (1,5h) e cada nível 1/2 custa TEMPO_PARADA_NORMAL_
# HORAS (~25min) -- ver estimar_tempo_rota. Isso deixa o número de
# nível 3 por rota subir quando sobra tempo (poucas paradas normais
# nessa rota) e descer quando não sobra (reduzindo o total de pedidos
# normais em vez de travar numa quantidade fixa de nível 3).
# NIVEL_3_TAMANHO_MAXIMO_ROTA continua existindo só como REFERÊNCIA (o
# valor típico de nível 3 numa rota cheia de tamanho_maximo, ver conta
# acima) -- reaproveitado por otimizacao_rotas.py (módulo de benchmark
# isolado da produção, sem a lógica de horas) e pelos badges do painel.
#
# Nível 4 nunca divide rota com nenhum pedido de nível 1/2/3 (ver
# NIVEL_ROTA_EXCLUSIVA) -- mas PODE dividir rota com OUTRO nível 4 do
# MESMO ENDEREÇO de entrega (embarcador igual ou diferente não
# importa), até NIVEL_4_TAMANHO_MAXIMO_ROTA (pedido do Hugo, 17/08 --
# substitui a regra anterior de "mesma rede/raiz de CNPJ", que juntava
# endereços diferentes da mesma empresa). Quando ambos têm agendamento
# (`scheduled_start`), a data também precisa bater; sem agendamento nos
# dois, junta só pelo endereço; um agendado + um sem agendamento nunca
# junta -- ver _chave_nivel4. Esse teto (nível 4) continua sendo por
# QUANTIDADE, não por horas -- é rota exclusiva, sem parada normal
# competindo pelo mesmo orçamento de tempo.
NIVEL_3_TAMANHO_MAXIMO_ROTA = 3
NIVEL_ROTA_EXCLUSIVA = 4
NIVEL_4_TAMANHO_MAXIMO_ROTA = 4
TEMPO_NIVEL3_HORAS = 1.5
TEMPO_PARADA_NORMAL_HORAS = 25 / 60
ROTA_TEMPO_MAXIMO_HORAS = 9.0


def estimar_tempo_rota(sublote: list[dict]) -> float:
    """Tempo estimado (horas) de uma rota mista de nível 1/2/3: cada
    nível 3 custa TEMPO_NIVEL3_HORAS e cada nível 1/2 custa TEMPO_
    PARADA_NORMAL_HORAS. Não é chamada para nível 4 (rota exclusiva,
    sem orçamento de horas -- ver NIVEL_ROTA_EXCLUSIVA)."""
    return sum(
        TEMPO_NIVEL3_HORAS if extrair_nivel_dificuldade(s) == 3 else TEMPO_PARADA_NORMAL_HORAS
        for s in sublote
    )


def separar_pedidos_exclusivos(servicos: list[dict], volume_maximo: int,
                               distancia_maxima_km: float | None, api_key: str | None,
                               tamanho_maximo_nivel4: int = NIVEL_4_TAMANHO_MAXIMO_ROTA,
                               distancia_maxima_viagem_km: float | None = None,
                               eh_viagem_fn=None) -> tuple[list[list[dict]], list[dict]]:
    """
    Pré-separa, ANTES do agrupamento geográfico específico de cada
    esquema de roteirização (grade, sweep, savings, cep, kmeans -- ver
    otimizacao_rotas.py), os pedidos que sempre saem "prontos" e não
    participam da comparação de proximidade de quem chama:
      - pedido "gigante" (mais caixas que `volume_maximo`): sempre
        isolado;
      - nível 4 sem par possível (nenhum outro nível 4 do MESMO
        endereço, com a mesma condição de agendamento -- ver
        _chave_nivel4): isolado, como sempre foi;
      - nível 4 do MESMO endereço de entrega (`address`) -- e, quando
        AMBOS têm agendamento (`scheduled_start`), também mesma data --
        agrupados entre si até `tamanho_maximo_nivel4`, respeitando a
        mesma trava de distância dos demais sublotes (Grande SP x
        Viagem, via `eh_viagem_fn`/`distancia_maxima_viagem_km`, igual
        aos outros modelos deste pacote); embarcador igual ou diferente
        não importa (pedido do Hugo, 17/08);
      - grupo de até 4 endereços diferentes (ou até 2, quando o volume
        já exige Truck -- ver regras/tipo_veiculo.py) cujo volume
        COMBINADO já justifica um veículo maior que o de última milha
        (pedido do Hugo, 15/08: "se tivermos 4 pedidos que juntos
        ultrapassem a quantidade mínima de caixas do veículo, vale mais
        a pena usar um carro maior") -- ver _extrair_grupos_veiculo_grande,
        abaixo. Mesmo endereço nunca conta mais de 1 vez contra esse
        limite (pode ter qualquer quantidade de pedidos).

    Devolve (sublotes_prontos, demais) -- `demais` (nível 1/2/3, fora de
    qualquer grupo de veículo grande) é o que quem chama deve agrupar
    com o algoritmo próprio de cada esquema.

    Compartilhada por TODOS os esquemas de roteirização (pedido do
    Hugo, 15/08: a junção de nível 4 vale igual pros 5, não só pro
    modelo Atual -- evita 5 implementações divergentes da mesma regra
    de negócio).
    """
    def _limite(sublote_candidato: list[dict]) -> float | None:
        if eh_viagem_fn is not None and eh_viagem_fn(sublote_candidato):
            return distancia_maxima_viagem_km
        return distancia_maxima_km

    def _cabe_na_distancia(servico: dict, sublote_atual: list[dict]) -> bool:
        limite = _limite(sublote_atual + [servico])
        if limite is None:
            return True
        coords_novo = obter_coordenadas(servico, api_key)
        if not coords_novo:
            return True
        for outro in sublote_atual:
            coords_outro = obter_coordenadas(outro, api_key)
            if coords_outro and _distancia_km(*coords_novo, *coords_outro) > limite:
                return False
        return True

    def _chave_ordenacao(servico: dict):
        coords = obter_coordenadas(servico, api_key)
        if coords:
            return (0, coords[0], coords[1])
        cep = extrair_cep(servico)
        return (1, int(cep) if cep else float("inf"), 0.0)

    def _empacotar_grupo(servicos_grupo: list[dict]) -> list[list[dict]]:
        ordenados_grupo = sorted(servicos_grupo, key=_chave_ordenacao)
        sublotes_grupo: list[list[dict]] = []
        atual: list[dict] = []
        caixas = 0
        for servico in ordenados_grupo:
            cx_pedido = extrair_volume_caixas(servico)
            cabe_entregas = len(atual) + 1 <= tamanho_maximo_nivel4
            cabe_caixas = caixas + cx_pedido <= volume_maximo
            cabe_distancia = _cabe_na_distancia(servico, atual)
            if atual and not (cabe_entregas and cabe_caixas and cabe_distancia):
                sublotes_grupo.append(atual)
                atual = []
                caixas = 0
            atual.append(servico)
            caixas += cx_pedido
        if atual:
            sublotes_grupo.append(atual)
        return sublotes_grupo

    def _agrupar_por_endereco(candidatos: list[dict]) -> list[dict]:
        """Agrupa `candidatos` por endereço (campo 'address') -- cada
        grupo é 1 'unidade' pro empacotamento de veículo grande abaixo
        (mesmo endereço nunca conta mais de 1 vez, não importa quantos
        pedidos tenha). Ordenado por caixas DECRESCENTE: a semente de
        cada cluster é sempre o maior endereço ainda não usado, o mais
        provável de precisar de um veículo maior."""
        por_endereco: dict[object, list[dict]] = defaultdict(list)
        for s in candidatos:
            por_endereco[s.get("address")].append(s)
        grupos = [
            {"pedidos": pedidos, "caixas": sum(extrair_volume_caixas(s) for s in pedidos)}
            for pedidos in por_endereco.values()
        ]
        grupos.sort(key=lambda g: -g["caixas"])
        return grupos

    def _distancia_para_cluster(pedidos_cluster: list[dict], grupo_candidato: dict) -> float:
        centroide = _centroide_coords(pedidos_cluster, api_key)
        coords_candidato = obter_coordenadas(grupo_candidato["pedidos"][0], api_key)
        if centroide and coords_candidato:
            return _distancia_km(*centroide, *coords_candidato)
        return 0.0  # sem coordenada -- não dá pra medir, mantém a ordem por caixas (não bloqueia)

    def _extrair_grupos_veiculo_grande(candidatos: list[dict]) -> tuple[list[list[dict]], list[dict]]:
        """
        Consolida pedidos de até 4 endereços diferentes (ou até 2, se o
        volume só couber em Truck) cujo volume COMBINADO já classifica
        em algum tipo de veículo grande (regras.tipo_veiculo.
        classificar_tipo_veiculo) -- ver docstring de
        separar_pedidos_exclusivos.

        Guloso: parte do endereço com mais caixas ainda não usado e vai
        anexando o endereço geograficamente mais próximo ainda não
        usado, enquanto o total ainda couber em ALGUM tipo (teto que
        ENCOLHE conforme mais endereços entram -- com 3+ endereços,
        Truck deixa de ser alcançável e o teto cai de 2500 pra 1200, ver
        regras.tipo_veiculo.teto_caixas_para_enderecos) e a distância
        pro cluster continuar dentro da mesma trava de sempre
        (`_cabe_na_distancia`, Grande SP x Viagem).

        Guarda o ÚLTIMO estado em que o cluster classificou em algum
        tipo (`melhor`) -- crescer mais um endereço pode "estourar" o
        cluster pra fora de qualquer faixa válida (ex: 3 endereços/
        1200cx cabe em 3/4, mas o 4º endereço empurra pra 1250cx, que
        não cabe nem em 3/4 nem em Truck com 4 endereços); nesse caso
        o cluster extraído é o último válido, não o final.

        Extrai o cluster (`melhor`) ao final se ele classificou em
        algum momento -- inclusive com 1 endereço só (vários pedidos
        pro MESMO endereço somando volume de veículo grande, nenhum
        deles "gigante" individualmente). Endereço(s) que nunca entram
        em nenhum cluster válido voltam pro pool comum (`sobras`).
        """
        grupos = _agrupar_por_endereco(candidatos)
        usados: set[int] = set()
        extraidos: list[list[dict]] = []

        for i, semente in enumerate(grupos):
            if i in usados:
                continue
            indices_cluster = [i]
            pedidos_cluster = list(semente["pedidos"])
            caixas_cluster = semente["caixas"]
            melhor = None
            if classificar_tipo_veiculo(caixas_cluster, 1) is not None:
                melhor = (list(indices_cluster), list(pedidos_cluster))

            while True:
                teto = teto_caixas_para_enderecos(len(indices_cluster) + 1)
                if teto == 0 or caixas_cluster >= teto:
                    break
                candidatas = [
                    (j, g) for j, g in enumerate(grupos)
                    if j not in usados and j not in indices_cluster
                    and caixas_cluster + g["caixas"] <= teto
                    and all(_cabe_na_distancia(s, pedidos_cluster) for s in g["pedidos"])
                ]
                if not candidatas:
                    break
                proximo_j, proximo = min(candidatas, key=lambda par: _distancia_para_cluster(pedidos_cluster, par[1]))
                indices_cluster.append(proximo_j)
                pedidos_cluster = pedidos_cluster + proximo["pedidos"]
                caixas_cluster += proximo["caixas"]
                if classificar_tipo_veiculo(caixas_cluster, len(indices_cluster)) is not None:
                    melhor = (list(indices_cluster), list(pedidos_cluster))

            if melhor is not None:
                indices_finais, pedidos_finais = melhor
                usados.update(indices_finais)
                extraidos.append(pedidos_finais)

        sobras = [s for j, g in enumerate(grupos) if j not in usados for s in g["pedidos"]]
        return extraidos, sobras

    gigantes: list[dict] = []
    grupos_nivel4: dict[tuple[object, object], list[dict]] = {}
    demais: list[dict] = []

    for servico in servicos:
        if extrair_volume_caixas(servico) > volume_maximo:
            gigantes.append(servico)
            continue
        if extrair_nivel_dificuldade(servico) == NIVEL_ROTA_EXCLUSIVA:
            grupos_nivel4.setdefault(_chave_nivel4(servico), []).append(servico)
            continue
        demais.append(servico)

    grupos_veiculo_grande, demais = _extrair_grupos_veiculo_grande(demais)

    sublotes_prontos: list[list[dict]] = [[s] for s in gigantes]
    for grupo in grupos_nivel4.values():
        sublotes_prontos.extend(_empacotar_grupo(grupo))
    sublotes_prontos.extend(grupos_veiculo_grande)

    return sublotes_prontos, demais


def dividir_em_sublotes(servicos: list[dict], tamanho_minimo: int = 10, tamanho_maximo: int = 18,
                        volume_maximo: int = 100, distancia_maxima_km: float | None = 15,
                        api_key: str | None = None) -> list[list[dict]]:
    """
    Divide uma região grande em sublotes respeitando CINCO travas ao
    mesmo tempo (pedido do Hugo, 09/08: "no máximo 18 entregas OU 100
    caixas por rota, o que vier primeiro" -- e depois, 09/08: "máximo
    de 15km de distância entre pedidos da mesma rota", achado ao
    revisar a rota #6 do dia, que tinha pego um pedido de Niterói-RJ
    junto com pedidos de São Paulo por ser "a rota mais próxima com
    espaço", mesmo estando a mais de 300km; 10/08: nível de dificuldade
    da entrega, ver extrair_nivel_dificuldade; e 20/08: orçamento de
    horas da rota, ver estimar_tempo_rota):
      - até `tamanho_maximo` entregas por sublote (18 por padrão),
        preenchido normalmente por nível 1/2/3; entrega nível 4 nunca
        divide sublote com mais ninguém (rota exclusiva, mesmo
        tratamento do pedido "gigante" de caixas, abaixo);
      - até `volume_maximo` caixas (soma de extrair_volume_caixas) por
        sublote;
      - nenhum par de pedidos do MESMO sublote pode estar a mais de
        `distancia_maxima_km` um do outro (quando ambos têm
        coordenada -- sem coordenada não dá pra checar, não bloqueia).
        `distancia_maxima_km=None` desliga essa trava por completo
        (pedido do Hugo, 10/08: rotas de Viagem não têm limite de
        distância -- só as outras travas de tamanho/volume/nível valem);
      - tempo estimado da rota (estimar_tempo_rota) até
        ROTA_TEMPO_MAXIMO_HORAS (9h): cada nível 3 custa
        TEMPO_NIVEL3_HORAS (1,5h) e cada nível 1/2 custa TEMPO_PARADA_
        NORMAL_HORAS (~25min) -- ajustado 15/08 e substituído 20/08
        (antes: teto FIXO de NIVEL_3_TAMANHO_MAXIMO_ROTA pedidos nível 3
        por rota; achado real, 15/08, que motivou tirar o teto da rota
        INTEIRA: 24 das 36 rotas do dia (67%) saíam travadas em 4 por
        causa disso, muitas com só 1 pedido nível-3 "puxando" e
        descartando o resto da vizinhança geográfica fácil; agora o
        teto por QUANTIDADE virou um orçamento por TEMPO -- uma rota
        com mais nível 3 cabe, mas com menos pedidos normais pra
        compensar, e uma rota só de nível 1/2 nunca esbarra nele, 14
        paradas * ~25min = ~5,8h < 9h).

    Considera PROXIMIDADE real: ordena os serviços por coordenada
    (lat, lng) quando disponível antes de dividir, pra que cada
    sublote fique com pedidos geograficamente vizinhos entre si; cai
    pra CEP completo como reserva quando não há coordenada. Sobre essa
    ordem já próxima, empacota de forma GANANCIOSA (greedy bin
    packing): vai enchendo o sublote atual até que o próximo pedido
    estoure uma das travas, aí fecha o sublote e abre outro -- isso
    tende a aproximar cada rota do limite (18, 100 ou 9h), sem nunca
    estourar nenhuma das travas.

    Exceção de pedido gigante: um único pedido com mais de
    `volume_maximo` caixas nunca cabe junto com nenhum outro -- aloca
    uma rota exclusiva isolada só pra ele.

    Nível 4 (pedido do Hugo, 10/08 -- ajustado 15/08 e 17/08): nunca
    divide rota com pedido de nível 1/2/3. Mas PODE dividir rota com
    OUTRO nível 4 do MESMO endereço de entrega (e mesma data de
    agendamento, quando ambos têm agendamento) -- ver _chave_nivel4 e
    separar_pedidos_exclusivos, chamada abaixo, compartilhada por TODOS
    os esquemas de roteirização (não só este).
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

    sublotes_prontos, demais = separar_pedidos_exclusivos(
        servicos, volume_maximo, distancia_maxima_km, api_key,
    )

    ordenados = sorted(demais, key=_chave_ordenacao)

    sublotes: list[list[dict]] = []
    sublote_atual: list[dict] = []
    caixas_atual = 0

    for servico in ordenados:
        cx_pedido = extrair_volume_caixas(servico)
        nivel_pedido = extrair_nivel_dificuldade(servico)

        tempo_pedido = TEMPO_NIVEL3_HORAS if nivel_pedido == 3 else TEMPO_PARADA_NORMAL_HORAS
        cabe_tempo = estimar_tempo_rota(sublote_atual) + tempo_pedido <= ROTA_TEMPO_MAXIMO_HORAS

        cabe_entregas = len(sublote_atual) + 1 <= tamanho_maximo
        cabe_caixas = caixas_atual + cx_pedido <= volume_maximo
        cabe_distancia = _cabe_na_distancia(servico, sublote_atual)

        if sublote_atual and not (cabe_entregas and cabe_caixas and cabe_distancia and cabe_tempo):
            sublotes.append(sublote_atual)
            sublote_atual = []
            caixas_atual = 0

        sublote_atual.append(servico)
        caixas_atual += cx_pedido

    if sublote_atual:
        sublotes.append(sublote_atual)

    sublotes.extend(sublotes_prontos)

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
