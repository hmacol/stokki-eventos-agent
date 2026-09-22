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
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from geocodificacao import geocodificar
from regioes_dia_fixo import RAIO_GRANDE_SP_KM, extrair_cidade, regiao_externa_da_cidade
from regras.tipo_veiculo import VOLUME_MAXIMO_GERAL_CX, classificar_tipo_veiculo

logger = logging.getLogger(__name__)

# Coordenada de referência do centro de São Paulo (Praça da Sé) -- a
# mesma já usada em alocacao_motoristas.py (COORD_BASE_SP) e zonas_sp.py
# (COORD_CENTRO_SP) como origem do raio da Grande SP.
COORD_CENTRO_SP = (-23.550520, -46.633309)

# Macro-regiões (pedido do Hugo, 12/08: "limitador dentro da Grande São
# Paulo... pedidos de Sorocaba não se misturariam com pedidos de Barueri
# automaticamente"): TRAVA RÍGIDA de partição -- nenhuma rota mistura
# pedidos de macro-regiões diferentes. As macros são: GRANDE_SP (dentro
# do raio de RAIO_GRANDE_SP_KM (35 km desde 20/08, ver regioes_dia_fixo.py)
# e fora de região externa), o NOME de cada região externa de dia fixo
# (Sorocaba, Campinas, Vale do Paraíba, Baixada Santista, Piracicaba --
# cada uma é uma direção/estrada diferente, também não se misturam ENTRE
# SI), e VIAGEM pra pedido a mais de RAIO_GRANDE_SP_KM (35 km desde
# 20/08, ver regioes_dia_fixo.py) sem região externa cadastrada.
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


def macro_regiao_predominante_do_sublote(sublote: list[dict], api_key: str | None = None) -> str:
    """Macro-região representativa de um sublote inteiro: a mais
    FREQUENTE (moda) entre macro_regiao_do_servico de cada item --
    mesmo padrão de zonas_sp.classificar_rota_zona (moda). Diferente de
    alocacao_motoristas.classificar_rota_viagem (1 entrega de fora já
    marca a rota inteira, trava de RISCO): aqui é predominância de
    ÁREA, pra decidir fusão pós-hoc entre sublotes já formados (Fase 1,
    22/08 -- ver criar_rotas_diarias.py::
    _fundir_sublotes_entre_macrorregioes). Sublote vazio: MACRO_GRANDE_SP
    (mesmo padrão seguro do resto do módulo)."""
    if not sublote:
        return MACRO_GRANDE_SP
    macros = [macro_regiao_do_servico(s, api_key) for s in sublote]
    return Counter(macros).most_common(1)[0][0]


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


def extrair_horario_atendimento(servico: dict) -> tuple[str, str]:
    """
    Horário padrão de atendimento (recebimento) do destinatário, formato
    "HH:MM" -- igual extrair_nivel_dificuldade, injetado no dict do
    serviço em criar_rotas_diarias.py (chaves '_horario_atendimento_
    inicio'/'_fim', via regras.complexidade_entrega). Sem essas chaves,
    assume o padrão mais amplo (00:00-23:59), mesmo piso que a VUUPT já
    usa pro horário de atendimento importado.
    """
    return (
        servico.get("_horario_atendimento_inicio") or "00:00",
        servico.get("_horario_atendimento_fim") or "23:59",
    )


def _chave_nivel4(servico: dict) -> tuple[object, object, object]:
    """Chave de junção de pedidos nível 4 -- ver separar_pedidos_exclusivos:
    (endereço, embarcador, dia de agendamento). Dois nível 4 só dividem
    rota com o MESMO endereço de entrega E o MESMO embarcador
    (`sender_id` -- pedido do Hugo, 09/09: nível 4 de embarcadores
    diferentes sai em rotas independentes, mesmo indo pro mesmo CD;
    até então embarcador não entrava na chave, regra de 17/08, que por
    sua vez substituiu a "mesma rede" por raiz de CNPJ). A data de
    agendamento só entra na comparação quando AMBOS têm agendamento.
    Sem agendamento (`_dia_agendamento` retorna None pros dois) junta
    só por endereço+embarcador; um agendado + um sem agendamento nunca
    junta (chaves com `dia` None x data nunca batem), mesmo endereço
    igual."""
    return (servico.get("address"), servico.get("sender_id"), _dia_agendamento(servico))


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


def coordenada_embutida(servico: dict) -> tuple[float, float] | None:
    """(lat, lng) das chaves latitude/longitude do proprio dict (servico
    vindo de rota existente, rascunho ou replay), ou None se ausentes,
    nao numericas ou (0, 0) -- placeholder que a Vuupt/geocache usam
    pra "sem coordenada"."""
    lat, lng = servico.get("latitude"), servico.get("longitude")
    if lat in (None, "") or lng in (None, ""):
        return None
    try:
        par = (float(lat), float(lng))
    except (TypeError, ValueError):
        return None
    return None if par == (0.0, 0.0) else par


def obter_coordenadas(servico: dict, api_key: str | None) -> tuple[float, float] | None:
    """
    Coordenada do servico: primeiro a EMBUTIDA no dict (latitude/
    longitude -- desde 18/09, mesma preferencia que coords_do_servico e
    otimizacao_rotas._distancia_da_base ja tinham; assim agrupadores,
    sequenciador e replay enxergam a mesma coordenada), senao geocodifica
    o 'address' reaproveitando o MESMO cache de geocodificacao usado pelo
    pipeline de importacao (geocodificacao.py, tabela geocache em
    dados/dados.db) -- a imensa maioria dos pedidos not_assigned ja foi
    geocodificada por la, isso normalmente e um cache hit, sem chamada
    nova no Google Maps. Retorna None se nao houver endereco, chave de
    API, ou coordenada (cache miss + falha do Google).
    """
    embutida = coordenada_embutida(servico)
    if embutida:
        return embutida
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
    base -> p1 -> ... -> pN, SEM a perna de volta (desde 18/09: a rota
    real termina na ultima entrega -- ver COORDS_BASE/estimar_tempo_rota
    -- e o sequenciador otimiza o mesmo objetivo; ate entao somava uma
    volta ficticia a base). Servico sem coordenada e ignorado no
    somatorio. Usada por selecao_modelo._km_total, pelo km dos rascunhos
    (painel_agentes/rascunhos_rota.py) e pelo polimento entre rotas.
    """
    coords = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
    if not coords:
        return 0.0
    total = _distancia_km(base_lat, base_lng, *coords[0])
    for i in range(len(coords) - 1):
        total += _distancia_km(*coords[i], *coords[i + 1])
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


def _cabe_na_distancia_par(servico: dict, sublote_atual: list[dict],
                           distancia_maxima_km: float | None, api_key: str | None) -> bool:
    """Teste PAR-A-PAR (servico contra CADA item já em sublote_atual) --
    extraído do nested _cabe_na_distancia de dividir_em_sublotes (Fase
    1, 22/08) pra ser reaproveitado por fundir_sublotes_pequenos, que
    testa vários pedidos de uma vez contra um receptor."""
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


def _km_acumulado_sequencial(servicos: list[dict], api_key: str | None) -> float:
    """Soma das distâncias entre itens CONSECUTIVOS da lista, na ordem
    em que aparecem -- SEM a perna até/da base. Aproximação do trajeto
    acumulado (salvaguarda contra zigzag que a trava par-a-par não
    pega), não a métrica final pós-2opt (ver calcular_km_estimado pra
    essa). Item sem coordenada não conta na soma (mesmo padrão seguro
    do resto do módulo). Fase 1, 22/08."""
    coords = [c for c in (obter_coordenadas(s, api_key) for s in servicos) if c]
    total = 0.0
    for i in range(len(coords) - 1):
        total += _distancia_km(*coords[i], *coords[i + 1])
    return total


def fundir_sublotes_pequenos(
    sublotes: list[list[dict]], tamanho_minimo: int, tamanho_maximo: int,
    volume_maximo: int, api_key: str | None = None,
    distancia_maxima_km: float | None = None,
    distancia_maxima_viagem_km: float | None = None,
    km_acumulado_maximo: float | None = None,
    km_acumulado_maximo_viagem: float | None = None,
    eh_viagem_fn=None,
    compativel=None,
) -> list[list[dict]]:
    """Pós-processamento (roda 1x, depois que TODOS os sublotes já
    fecharam pela regra gulosa de sempre): tenta fundir cada sublote
    abaixo de tamanho_minimo com o sublote MAIS PRÓXIMO (por centroide,
    mesma primitiva de consolidar_regioes_pequenas) entre TODOS os
    outros sublotes -- não só o antecessor imediato na ordem de
    fechamento. Fundir só com o antecessor imediato NÃO FUNCIONA aqui:
    por construção do laço guloso, o item que disparou o fechamento de
    um sublote já falhou EXATAMENTE contra esse mesmo sublote (senão o
    laço teria aceitado o item em vez de fechar) -- testar de novo dá
    sempre o mesmo resultado (monotonicidade: mais itens só pioram
    caixas/tempo/distância, nunca melhoram). Tenta o próximo candidato
    mais próximo se o mais perto não couber, mesmo padrão de
    consolidar_regioes_pequenas.

    Corrige bug achado 22/08: tamanho_minimo era parâmetro de
    dividir_em_sublotes mas nunca era lido -- um sublote podia sair com
    1-2 pedidos sem aviso, sem tentativa de recuperação, quando as
    travas de tamanho/caixas/distância/tempo forçavam quebra DEPOIS da
    consolidação por região (consolidar_regioes_pequenas), que só
    garante o mínimo até ali.

    Extraída (Fase 1, 22/08) do nested _tenta_fundir_pequenos de
    dividir_em_sublotes -- função PÚBLICA de módulo pra ser reaproveitada
    pela fusão pós-hoc entre macro-regiões de uma partição inteira (ver
    criar_rotas_diarias.py::_fundir_sublotes_entre_macrorregioes).
    Generalizada com 2 parâmetros novos, opcionais:
      - eh_viagem_fn(sublote_candidato) -> bool (opcional): decide por
        RECEPTOR se usa o teto de Viagem (distancia_maxima_viagem_km/
        km_acumulado_maximo_viagem) ou o de Grande SP -- mesmo padrão
        já usado em escolher_melhor_modelo/separar_pedidos_exclusivos.
        Sem isso (None, padrão), sempre usa distancia_maxima_km/
        km_acumulado_maximo -- é o comportamento de dividir_em_
        sublotes, uma única macro-região por chamada;
      - compativel(pequeno, candidato) -> bool (opcional): predicado
        extra -- só tenta fundir esse par se retornar True, além das
        travas numéricas de sempre. Sem isso (None), qualquer par pode
        tentar, como sempre foi.

    Nível 4 é rota exclusiva (NIVEL_ROTA_EXCLUSIVA): sublote que tenha
    qualquer pedido nível 4 nunca entra numa fusão, nem como "pequeno"
    nem como receptor (pedido do Hugo, 09/09 -- achado real: a fusão
    pós-hoc entre macro-regiões juntou um nível 4 de 1 caixa com um
    nível 4 de 75 caixas de OUTRO embarcador, porque só as travas
    numéricas eram checadas). Sublote de veículo grande também fica de
    fora (as travas dele são as do próprio tipo, não as de última
    milha -- na prática o teto de `volume_maximo` já barrava)."""
    def _exclusivo(sublote: list[dict]) -> bool:
        if any(extrair_nivel_dificuldade(s) == NIVEL_ROTA_EXCLUSIVA for s in sublote):
            return True
        return classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None

    def _limite(sublote_candidato: list[dict]) -> tuple[float | None, float | None]:
        eh_viagem = eh_viagem_fn is not None and eh_viagem_fn(sublote_candidato)
        dist = distancia_maxima_viagem_km if eh_viagem else distancia_maxima_km
        km_acum = km_acumulado_maximo_viagem if eh_viagem else km_acumulado_maximo
        return dist, km_acum

    resultado = list(sublotes)
    i = 0
    while i < len(resultado):
        pequeno = resultado[i]
        if len(pequeno) >= tamanho_minimo or _exclusivo(pequeno):
            i += 1
            continue
        centro_pequeno = _centroide_coords(pequeno, api_key)
        outros = [j for j in range(len(resultado)) if j != i and not _exclusivo(resultado[j])]
        if centro_pequeno:
            outros.sort(key=lambda j: (
                _distancia_km(*centro_pequeno, *_centroide_coords(resultado[j], api_key))
                if _centroide_coords(resultado[j], api_key) else float("inf")
            ))
        fundiu = False
        for j in outros:
            receptor = resultado[j]
            if compativel is not None and not compativel(pequeno, receptor):
                continue
            if len(receptor) + len(pequeno) > tamanho_maximo:
                continue
            if sum(extrair_volume_caixas(s) for s in receptor + pequeno) > volume_maximo:
                continue
            candidata = receptor + pequeno
            if (estimar_tempo_rota(candidata, api_key) > ROTA_TEMPO_MAXIMO_HORAS
                    and not _orcamento_inviavel_por_distancia(candidata, api_key)):
                continue
            if not janela_viavel(candidata, api_key):
                continue
            distancia_max, km_acumulado_max = _limite(receptor + pequeno)
            if distancia_max is not None and any(
                not _cabe_na_distancia_par(s, receptor, distancia_max, api_key) for s in pequeno
            ):
                continue
            if km_acumulado_max is not None and _km_acumulado_sequencial(receptor + pequeno, api_key) > km_acumulado_max:
                continue
            receptor.extend(pequeno)
            del resultado[i]
            fundiu = True
            break
        if not fundiu:
            i += 1
    return resultado


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
# TEMPO_NIVEL3_HORAS e cada nível 1/2 custa TEMPO_PARADA_NORMAL_HORAS
# (ambos calibrados pela execução real em 25/08, ver bloco de constantes
# abaixo), MAIS o deslocamento estimado: perna base -> 1ª parada e pernas
# entre paradas, haversine x FATOR_ESTRADA, a VELOCIDADE_MEDIA_KMH (ou
# VELOCIDADE_RODOVIA_KMH em perna longa) -- ver estimar_tempo_rota. Isso deixa o número de
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
# MESMO ENDEREÇO de entrega e do MESMO EMBARCADOR (pedido do Hugo,
# 09/09: embarcadores diferentes saem em rotas independentes; 17/08:
# junção por endereço substituiu a regra anterior de "mesma rede/raiz
# de CNPJ", que juntava endereços diferentes da mesma empresa). Quando
# ambos têm agendamento (`scheduled_start`), a data também precisa
# bater; sem agendamento nos dois, junta só pelo endereço+embarcador;
# um agendado + um sem agendamento nunca junta -- ver _chave_nivel4.
# Grupo cujo volume SOMADO já cabe num tipo de veículo grande (regras/
# tipo_veiculo.py) sai como 1 rota só desse tipo, sem teto de 100
# caixas nem de NIVEL_4_TAMANHO_MAXIMO_ROTA pedidos (pedido do Hugo,
# 09/09 -- caso real: 5 pedidos KHAPPY pro CD do GPA, 573 caixas, saíam
# em 5 rotas e eram juntados à mão todo dia). Grupo que NÃO chega a
# veículo grande segue a regra de última milha de sempre: até
# NIVEL_4_TAMANHO_MAXIMO_ROTA pedidos e 100 caixas, "gigante" isolado.
# Esse teto (nível 4) continua sendo por QUANTIDADE, não por horas --
# é rota exclusiva, sem parada normal competindo pelo mesmo orçamento
# de tempo.
NIVEL_3_TAMANHO_MAXIMO_ROTA = 3
NIVEL_ROTA_EXCLUSIVA = 4
NIVEL_4_TAMANHO_MAXIMO_ROTA = 4
# Calibração pela execução REAL de 60 dias (análise 25/08: 5.180
# entregas, 546 rotas finalizadas, timestamps de chegada/conclusão da
# VUUPT descartando as confirmações em lote):
#   - parada nível 3: mediana 36,8min, p75 72,6min -> 1h15 (antes 2h,
#     ~3x acima do real);
#   - parada nível 1/2: mediana 9,6min, p75 23,9min -> 15min (antes 25min);
#   - velocidade entre paradas: mediana real 15,4 km/h -> 15 (antes 18,
#     que era OTIMISTA, não conservador);
#   - orçamento de 9h coincide com o p90 da duração útil real (9,05h):
#     bem calibrado, mantido.
# Antes de 25/08 os erros se cancelavam: nível 3 inflado compensava a
# perna da base que ficava fora da conta (ver COORDS_BASE abaixo). Os
# quatro ajustes andam JUNTOS -- corrigir só um desequilibra (só a base:
# metade das rotas reais de 6h seria rejeitada; só o nível 3: 29% das
# rotas de ida longa ficam subestimadas).
TEMPO_NIVEL3_HORAS = 1.25
TEMPO_PARADA_NORMAL_HORAS = 15 / 60
ROTA_TEMPO_MAXIMO_HORAS = 9.0
VELOCIDADE_MEDIA_KMH = 15.0
# Perna longa (haversine acima de PERNA_RODOVIA_KM) anda em rodovia:
# sem isso um pedido Viagem a 200 km "custaria" 17h a 15 km/h e nunca
# caberia em rota nenhuma (achado da simulação de 25/08).
VELOCIDADE_RODOVIA_KMH = 60.0
PERNA_RODOVIA_KM = 30.0
# Haversine subestima a estrada: fator medido nas rotas reais (km
# rodado pela VUUPT / haversine da sequência executada, mediana 1,3;
# medindo só entre paradas o real chega a 2,5x -- a perna da base é
# metade do trajeto real).
FATOR_ESTRADA = 1.3
# Coordenada da base (Rua Zilda): quem já geocodificou (criar_rotas_
# diarias, selecao_modelo, painel de planejamento) registra uma vez por
# processo via definir_coords_base. Com ela, estimar_tempo_rota inclui a
# perna base -> 1ª parada (mediana 0,64h, p90 2,3h na execução real --
# ficava de fora até 25/08). Sem ela o estimador segue sem essa perna
# (nunca quebra, só fica otimista). A perna de VOLTA fica de fora de
# propósito, por regra de NEGÓCIO (pedido do Hugo, 26/08), não só por
# calibração: numa rota que não é Viagem, o motorista só volta pro
# galpão se algo deu errado (insucesso com produto pra devolver) --
# não faz parte da jornada normal, então não deve contar no orçamento
# de horas. Bate com o dado real: a duração calibrada (p90 9,05h) foi
# medida do início até a ÚLTIMA ENTREGA CONCLUÍDA, nunca até a volta.
COORDS_BASE: tuple[float, float] | None = None


def definir_coords_base(lat: float, lng: float) -> None:
    """Registra a coordenada da base pro estimador de horas deste
    processo (ver COORDS_BASE)."""
    global COORDS_BASE
    COORDS_BASE = (float(lat), float(lng))


def _tempo_perna_horas(km_haversine: float) -> float:
    """Horas de UMA perna: haversine x FATOR_ESTRADA, os primeiros
    PERNA_RODOVIA_KM a VELOCIDADE_MEDIA_KMH (urbana) e o excedente a
    VELOCIDADE_RODOVIA_KMH -- MISTURA contínua, não um degrau (corrigido
    25/08, achado da revisão adversarial: a versão anterior trocava de
    velocidade de uma vez só EXATAMENTE em PERNA_RODOVIA_KM, criando uma
    descontinuidade de ~4x -- uma perna de 29,9km custava mais que uma
    de 30,1km, e o estimador deixava de ser monotônico: adicionar um
    pedido mais distante podia REDUZIR o tempo estimado da rota,
    distorcendo as travas de formação/reparo pra rotas com 1ª parada
    entre ~25 e 30km da base (Cotia, Barueri, Itaquá). Contínua em
    km_haversine == PERNA_RODOVIA_KM (ambos os lados dão o mesmo valor
    ali) e estritamente crescente em km_haversine -- ver
    test_perna_horas_e_continua_e_monotona."""
    urbano_km = min(km_haversine, PERNA_RODOVIA_KM)
    rodovia_km = max(km_haversine - PERNA_RODOVIA_KM, 0.0)
    return (urbano_km / VELOCIDADE_MEDIA_KMH + rodovia_km / VELOCIDADE_RODOVIA_KMH) * FATOR_ESTRADA


def _orcamento_inviavel_por_distancia(sublote: list[dict], api_key: str | None = None,
                                      coords_base: tuple[float, float] | None = None) -> bool:
    """True quando NENHUM agrupamento possível faria este sublote caber
    em ROTA_TEMPO_MAXIMO_HORAS: pelo menos um pedido, SOZINHO (só ele +
    a perna da base), já estoura o orçamento por pura DISTÂNCIA até a
    base -- destino muito longe (ex.: seleção manual fora da área usual
    de atendimento), não excesso de paradas. Qualquer rota que inclua
    esse pedido paga, em algum trecho, um deslocamento pelo menos tão
    longo quanto a perna base -> pedido (desigualdade triangular), então
    fragmentar em rotas de 1 pedido não resolve
    nada (cada uma continuaria acima do orçamento) e só multiplica
    motoristas pro mesmo problema (achado da revisão de 25/08: N
    pedidos vizinhos e distantes viravam N rotas de 1, todas ainda
    acima de 9h). Nesse caso o orçamento de horas não deve bloquear o
    agrupamento/reprovar o candidato -- as outras travas (distância
    par-a-par, volume, tamanho) continuam valendo normalmente; usada por
    dividir_em_sublotes, _empacotar_ganancioso, agrupar_por_savings,
    fundir_sublotes_pequenos e exige_orcamento_horas pra não divergirem."""
    return any(
        estimar_tempo_rota([s], api_key, coords_base) > ROTA_TEMPO_MAXIMO_HORAS
        for s in sublote
    )


def estimar_tempo_rota(sublote: list[dict], api_key: str | None = None,
                       coords_base: tuple[float, float] | None = None,
                       coords_fn=None) -> float:
    """Tempo estimado (horas) de uma rota de nível 1/2/3 até a ÚLTIMA
    entrega: tempo de PARADA (nível 3 custa TEMPO_NIVEL3_HORAS, nível
    1/2 custa TEMPO_PARADA_NORMAL_HORAS) + DESLOCAMENTO: perna base ->
    1ª parada (quando `coords_base` ou COORDS_BASE existe) e pernas
    entre itens consecutivos na ordem dada (ordem de FORMAÇÃO em
    dividir_em_sublotes/_fusao_valida; ordem final pós-2opt em
    selecao_modelo._validar), cada perna por _tempo_perna_horas.

    `coords_fn(servico) -> (lat, lng) | None` (opcional) substitui
    obter_coordenadas -- pro painel de planejamento, cujas paradas já
    carregam latitude/longitude e não têm o endereço na forma que o
    cache de geocodificação conhece (evita geocodificar de novo).

    Serviço sem coordenada não conta no deslocamento (mesmo padrão
    seguro do resto do módulo) -- chamada sem coordenada nenhuma nunca
    quebra, só fica sem a parcela de deslocamento. Não é chamada para
    nível 4 (rota exclusiva, sem orçamento de horas -- ver
    NIVEL_ROTA_EXCLUSIVA e exige_orcamento_horas)."""
    tempo_paradas = sum(
        TEMPO_NIVEL3_HORAS if extrair_nivel_dificuldade(s) == 3 else TEMPO_PARADA_NORMAL_HORAS
        for s in sublote
    )
    resolver = coords_fn or (lambda s: obter_coordenadas(s, api_key))
    coords = [c for c in (resolver(s) for s in sublote) if c]
    base = coords_base or COORDS_BASE
    tempo_deslocamento = 0.0
    if coords and base:
        tempo_deslocamento += _tempo_perna_horas(_distancia_km(base[0], base[1], *coords[0]))
    for i in range(len(coords) - 1):
        tempo_deslocamento += _tempo_perna_horas(_distancia_km(*coords[i], *coords[i + 1]))
    return tempo_paradas + tempo_deslocamento


def exige_orcamento_horas(sublote: list[dict], api_key: str | None = None,
                          coords_base: tuple[float, float] | None = None) -> bool:
    """Rota que o orçamento de horas alcança: última milha com 2+
    paradas, sem nível 4 (rota exclusiva), não classificada como
    veículo grande (as travas dela são as do próprio tipo, ver
    _extrair_grupos_veiculo_grande) e não inviável só por distância (ver
    _orcamento_inviavel_por_distancia -- destino tão longe que nenhum
    agrupamento caberia mesmo, mesma exceção de "gigante" de caixas, só
    que por tempo). Mesmas exceções que dividir_em_sublotes já aplica ao
    pular os sublotes "prontos" de separar_pedidos_exclusivos --
    centralizadas aqui pra selecao_modelo._validar e
    reparar_sublotes_por_horas não divergirem."""
    if len(sublote) <= 1:
        return False
    if any(extrair_nivel_dificuldade(s) == NIVEL_ROTA_EXCLUSIVA for s in sublote):
        return False
    if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
        return False
    if _orcamento_inviavel_por_distancia(sublote, api_key, coords_base):
        return False
    return True


def reparar_sublotes_por_horas(sublotes: list[list[dict]], api_key: str | None = None,
                               coords_base: tuple[float, float] | None = None) -> tuple[list[list[dict]], int]:
    """Rede de segurança pós-sequenciamento (25/08): as travas de horas
    dos agrupadores estimam na ordem de FORMAÇÃO; o 2-opt depois muda a
    1ª parada (e portanto a perna da base), e uma rota pode ficar
    marginalmente acima do orçamento na ordem FINAL (achado da
    simulação: ~10% das rotas do savings, 9,1-9,9h). Quebra cada rota
    dessas, na ordem dada, em pedaços que cabem em
    ROTA_TEMPO_MAXIMO_HORAS. Devolve (sublotes, qtd_rotas_reparadas)."""
    resultado: list[list[dict]] = []
    reparadas = 0
    for sublote in sublotes:
        if (not exige_orcamento_horas(sublote)
                or (estimar_tempo_rota(sublote, api_key, coords_base) <= ROTA_TEMPO_MAXIMO_HORAS
                    and janela_respeitada(sublote, api_key, coords_base))):
            resultado.append(sublote)
            continue
        reparadas += 1
        atual: list[dict] = []
        for servico in sublote:
            candidato = atual + [servico]
            if atual and (estimar_tempo_rota(candidato, api_key, coords_base) > ROTA_TEMPO_MAXIMO_HORAS
                          or not janela_respeitada(candidato, api_key, coords_base)):
                resultado.append(atual)
                atual = []
            atual.append(servico)
        if atual:
            resultado.append(atual)
    return resultado, reparadas


# ── Janela de horário de entrega do cliente (pedido do Hugo, 09/09) ──────
# Até 09/09 a roteirização só olhava a DATA do agendamento
# (elegivel_para_data); a HORA era descartada em todo ponto do cálculo
# -- um cliente que "recebe só até 11h" podia sair como última parada
# de uma rota que começava às 10h. A partir daqui a janela entra em 3
# lugares: (1) sequenciamento (ordenar_com_janelas: 2-opt + realocação
# com objetivo km + atraso + espera), (2) formação/fusão de sublotes
# (janela_viavel: existe sequência que respeita as janelas?) e (3)
# orçamento de horas na ordem final (janela_respeitada, com as esperas
# até o cliente abrir contando na duração).
#
# Fonte da janela de cada pedido (resolver_janela/injetar_janelas):
#   1º agendamento CONFIRMADO com hora informada (agendamentos_pedido,
#      resposta do embarcador por e-mail / portal do cliente);
#   2º scheduled_start/end do serviço na Vuupt quando a hora é "real"
#      (não é um dos pares padrão que o pipeline chuta quando ninguém
#      informou hora -- ver JANELAS_PADRAO_IGNORADAS);
#   3º horário de atendimento do cadastro (planilha BD_CLIENTES/ajuste
#      manual da tela de Planejamento), '00:00-23:59' = sem janela.
# Quando há agendamento E cadastro, vale a interseção; interseção vazia
# -> vale o agendamento (o cliente confirmou aquela hora explicitamente).
#
# Hora de INÍCIO das rotas (BRT) -- fonte única do start_at que
# criar_rotas_diarias.py/rascunhos_rota.py gravam em toda rota
# (start_at_rota, abaixo) e da hora de saída do simulador de janelas.
# Hugo, 09/09: 06:00 (até então 10:00, o antigo "T13:00:00Z" fixo; a
# mediana real das rotas em nucleo_rotas era 09h-10h). Configurável em
# config.yaml (roteirizacao.hora_saida_base) via definir_hora_saida_base
# -- vale só pro simulador; o start_at segue HORA_INICIO_ROTA.
HORA_INICIO_ROTA = "06:00"
HORA_SAIDA_BASE = 6.0


def start_at_rota(data_alvo) -> str:
    """start_at da rota na Vuupt em UTC ("YYYY-MM-DDTHH:MM:SSZ") pra
    HORA_INICIO_ROTA em Brasília (UTC-3, sem horário de verão desde
    2019): 06:00 BRT -> "T09:00:00Z". `data_alvo` é um date."""
    horas = _hhmm_para_horas(HORA_INICIO_ROTA) + 3.0
    total = int(round(horas * 60))
    return f"{data_alvo.strftime('%Y-%m-%d')}T{total // 60:02d}:{total % 60:02d}:00Z"
TOLERANCIA_JANELA_HORAS = 0.25
# Objetivo do 2-opt com janelas, em "km equivalentes": 1h de atraso
# (chegar depois da janela fechar) custa 60 km; 1h esperando o cliente
# abrir custa 30 km (~2h de rodagem urbana a 15 km/h -- esperar parado
# é pior que rodar, mas atraso é o pior de todos).
PESO_ATRASO_JANELA_KM = 60.0
PESO_ESPERA_JANELA_KM = 30.0
# Pares (inicio, fim) que NÃO significam "o cliente pediu essa janela":
# são os padrões que pipeline.py (08:00-16:00), agendamento_confirmacao/
# atualizar_agendamentos_confirmados (08:00-18:00) e regioes_dia_fixo
# (08:00-16:00) gravam quando ninguém informou hora, mais o "dia todo".
JANELAS_PADRAO_IGNORADAS = {
    ("00:00", "23:59"), ("00:00", "00:00"), ("08:00", "18:00"), ("08:00", "16:00"),
}
FONTE_JANELA_AGENDAMENTO = "agendamento"
FONTE_JANELA_ATENDIMENTO = "atendimento"


def definir_hora_saida_base(hora: str | float | None) -> None:
    """Registra a hora de saída da base ("HH:MM" ou horas decimais) pro
    simulador deste processo (ver HORA_SAIDA_BASE). None/inválido: mantém."""
    global HORA_SAIDA_BASE
    if hora is None:
        return
    valor = hora if isinstance(hora, (int, float)) else _hhmm_para_horas(str(hora))
    if valor is not None:
        HORA_SAIDA_BASE = float(valor)


def _hhmm_para_horas(texto) -> float | None:
    """'14:30' -> 14.5; aceita '14:30:00', '8:00', '14h', '14h30'. None
    se não parsear."""
    if texto is None:
        return None
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2})(?::\d{2})?|h(\d{2})?)?\s*$", str(texto), re.IGNORECASE)
    if not m:
        return None
    horas = int(m.group(1))
    minutos = int(m.group(2) or m.group(3) or 0)
    if horas > 23 or minutos > 59:
        return None
    return horas + minutos / 60


def _horas_para_hhmm(horas: float) -> str:
    total = int(round(horas * 60))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def normalizar_hhmm(texto) -> str | None:
    """'14h' -> '14:00', '8:00' -> '08:00', '14:30:00' -> '14:30'; None
    quando não é hora reconhecível. Usada por quem grava horário vindo de
    texto livre (LLM, planilha) pra nunca persistir formato que
    _converter_data_para_iso não entende."""
    horas = _hhmm_para_horas(texto)
    return _horas_para_hhmm(horas) if horas is not None else None


def _janela_util(inicio, fim) -> tuple[str, str] | None:
    """Par ('HH:MM','HH:MM') só quando é uma janela REAL: as duas horas
    parseiam, início < fim e o par não é um dos padrões ignorados."""
    ini_txt, fim_txt = normalizar_hhmm(inicio), normalizar_hhmm(fim)
    if not ini_txt or not fim_txt:
        return None
    if (ini_txt, fim_txt) in JANELAS_PADRAO_IGNORADAS:
        return None
    if ini_txt == fim_txt:
        # "às 11h" gravado como 11:00-11:00 (scheduled_start == scheduled_end,
        # visto em produção): vale como janela de 1 hora a partir dali
        fim_txt = _horas_para_hhmm(min(_hhmm_para_horas(ini_txt) + 1.0, 23.98))
    if _hhmm_para_horas(ini_txt) >= _hhmm_para_horas(fim_txt):
        return None
    return ini_txt, fim_txt


def _hora_de_iso(texto) -> str | None:
    """'2026-09-10T14:00:00-03:00' -> '14:00' (a hora local do próprio
    campo, sem converter fuso); None se não parsear."""
    if not texto:
        return None
    try:
        return datetime.fromisoformat(str(texto)).strftime("%H:%M")
    except (ValueError, TypeError):
        return None


def carregar_janelas_confirmadas(db_path) -> dict[str, tuple[str, str]]:
    """{codigo_pedido: (inicio, fim)} dos agendamentos RESPONDIDOS em
    agendamentos_pedido cuja hora foi INFORMADA (as duas colunas de
    horário preenchidas -- desde 09/09 hora não informada fica NULL, em
    vez do chute 08:00-18:00). Falha de banco/tabela: {} (a roteirização
    segue só com as outras fontes)."""
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        rows = conn.execute(
            "SELECT pedido, horario_inicio_agendado, horario_fim_agendado FROM agendamentos_pedido "
            "WHERE status = 'RESPONDIDO' AND horario_inicio_agendado IS NOT NULL "
            "AND horario_fim_agendado IS NOT NULL"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.warning(f"Não consegui carregar as janelas confirmadas de agendamento: {e}")
        return {}
    janelas = {}
    for pedido, ini, fim in rows:
        janela = _janela_util(ini, fim)
        if janela and pedido:
            janelas[str(pedido).lstrip("#").strip()] = janela
    return janelas


def resolver_janela(servico: dict, janelas_confirmadas: dict[str, tuple[str, str]] | None = None
                    ) -> tuple[str | None, str | None, str | None]:
    """(inicio, fim, fonte) da janela efetiva do pedido -- ver o bloco de
    comentário acima. (None, None, None) = sem restrição de horário."""
    codigo = str(servico.get("code") or "").lstrip("#").strip()
    agendada = (janelas_confirmadas or {}).get(codigo)
    if not agendada:
        agendada = _janela_util(_hora_de_iso(servico.get("scheduled_start")),
                                _hora_de_iso(servico.get("scheduled_end")))
    atendimento = _janela_util(*extrair_horario_atendimento(servico))
    if not atendimento:
        cliente = servico.get("customer") or {}
        atendimento = _janela_util(cliente.get("operating_hour_start"), cliente.get("operating_hour_end"))

    if agendada and atendimento:
        ini = max(agendada[0], atendimento[0])
        fim = min(agendada[1], atendimento[1])
        if _hhmm_para_horas(ini) < _hhmm_para_horas(fim):
            return ini, fim, FONTE_JANELA_AGENDAMENTO
        return agendada[0], agendada[1], FONTE_JANELA_AGENDAMENTO
    if agendada:
        return agendada[0], agendada[1], FONTE_JANELA_AGENDAMENTO
    if atendimento:
        return atendimento[0], atendimento[1], FONTE_JANELA_ATENDIMENTO
    return None, None, None


def injetar_janelas(servicos: list[dict], janelas_confirmadas: dict[str, tuple[str, str]] | None = None) -> int:
    """Grava '_janela_inicio'/'_janela_fim'/'_janela_fonte' em cada dict
    de serviço (mesmo padrão de '_nivel_dificuldade'); chamar DEPOIS de
    injetar '_horario_atendimento_*'. Retorna quantos pedidos ficaram
    com janela."""
    com_janela = 0
    for s in servicos:
        ini, fim, fonte = resolver_janela(s, janelas_confirmadas)
        s["_janela_inicio"], s["_janela_fim"], s["_janela_fonte"] = ini, fim, fonte
        if ini:
            com_janela += 1
    return com_janela


def extrair_janela(servico: dict) -> tuple[float, float] | None:
    """Janela efetiva em horas decimais (inicio, fim), ou None quando o
    pedido não tem restrição de horário (sem chave injetada, formato
    inválido, ou 'dia todo')."""
    ini = _hhmm_para_horas(servico.get("_janela_inicio"))
    fim = _hhmm_para_horas(servico.get("_janela_fim"))
    if ini is None or fim is None or fim <= ini:
        return None
    if ini <= 0.0 and fim >= 23.98:
        return None
    return ini, fim


def tem_janela(sublote: list[dict]) -> bool:
    return any(extrair_janela(s) is not None for s in sublote)


def coords_do_servico(servico: dict, api_key: str | None = None) -> tuple[float, float] | None:
    """Mantida pelo nome (sequenciador, laboratorio, benchmark): desde
    18/09 obter_coordenadas ja prefere a coordenada embutida."""
    return obter_coordenadas(servico, api_key)


def simular_horarios(sublote: list[dict], api_key: str | None = None,
                     coords_base: tuple[float, float] | None = None,
                     coords_fn=None, hora_saida: float | None = None) -> dict:
    """Linha do tempo da rota na ORDEM DADA, com as mesmas premissas de
    estimar_tempo_rota (perna da base, _tempo_perna_horas, tempo de
    parada por nível; serviço sem coordenada não anda) MAIS as janelas:
    chegando antes da janela abrir o motorista ESPERA; chegando depois
    dela fechar conta ATRASO. Retorna:
      {"chegadas": [hora decimal de chegada por parada, na ordem],
       "atraso_h": soma dos atrasos, "espera_h": soma das esperas,
       "fora_janela": [índices das paradas atrasadas],
       "fim_h": hora da última entrega concluída,
       "duracao_h": fim_h - hora de saída}."""
    resolver = coords_fn or (lambda s: coords_do_servico(s, api_key))
    base = coords_base or COORDS_BASE
    t = HORA_SAIDA_BASE if hora_saida is None else hora_saida
    saida = t
    anterior = base
    chegadas: list[float] = []
    fora: list[int] = []
    atraso = espera = 0.0
    for idx, s in enumerate(sublote):
        c = resolver(s)
        if c and anterior:
            t += _tempo_perna_horas(_distancia_km(anterior[0], anterior[1], c[0], c[1]))
        if c:
            anterior = c
        chegadas.append(t)
        janela = extrair_janela(s)
        if janela:
            if t < janela[0]:
                espera += janela[0] - t
                t = janela[0]
            elif t > janela[1]:
                atraso += t - janela[1]
                fora.append(idx)
        t += TEMPO_NIVEL3_HORAS if extrair_nivel_dificuldade(s) == 3 else TEMPO_PARADA_NORMAL_HORAS
    return {"chegadas": chegadas, "atraso_h": atraso, "espera_h": espera,
            "fora_janela": fora, "fim_h": t, "duracao_h": t - saida}


def _atraso_intrinseco(sublote: list[dict], api_key=None, coords_base=None, coords_fn=None) -> float:
    """Atraso que nenhum agrupamento evita: pedido que, SOZINHO e saindo
    da base direto, já chega depois da própria janela (ex.: janela
    06:00-08:00 com saída às 10h). Paralelo de _orcamento_inviavel_por_
    distancia -- fragmentar não resolve, então não deve bloquear."""
    return sum(
        simular_horarios([s], api_key, coords_base, coords_fn)["atraso_h"]
        for s in sublote if extrair_janela(s) is not None
    )


def janela_respeitada(sublote: list[dict], api_key: str | None = None,
                      coords_base: tuple[float, float] | None = None, coords_fn=None) -> bool:
    """Trava de janela na ORDEM DADA (ordem final, pós-sequenciamento):
    atraso evitável dentro da tolerância E duração com esperas dentro
    do orçamento de horas (exceto rota já inviável só por distância).
    Sem janela em nenhum pedido: True sem custo nenhum."""
    if len(sublote) <= 1 or not tem_janela(sublote):
        return True
    sim = simular_horarios(sublote, api_key, coords_base, coords_fn)
    if sim["atraso_h"] - _atraso_intrinseco(sublote, api_key, coords_base, coords_fn) > TOLERANCIA_JANELA_HORAS:
        return False
    if (sim["duracao_h"] > ROTA_TEMPO_MAXIMO_HORAS
            and not _orcamento_inviavel_por_distancia(sublote, api_key, coords_base)):
        return False
    return True


def janela_viavel(sublote: list[dict], api_key: str | None = None,
                  coords_base: tuple[float, float] | None = None, coords_fn=None) -> bool:
    """Trava de janela pra FORMAÇÃO/FUSÃO de sublotes (dividir_em_
    sublotes, _empacotar_ganancioso, _fusao_valida do savings,
    fundir_sublotes_pequenos): existe alguma sequência -- a que
    ordenar_com_janelas encontra -- que respeite as janelas? A ordem de
    formação não serve pra julgar (o 2-opt reordena depois). Sem base
    registrada não dá pra sequenciar: julga na ordem dada."""
    if len(sublote) <= 1 or not tem_janela(sublote):
        return True
    base = coords_base or COORDS_BASE
    ordem = ordenar_com_janelas(sublote, base[0], base[1], api_key, coords_fn) if base else list(sublote)
    return janela_respeitada(ordem, api_key, base, coords_fn)


MAX_ITERACOES_2OPT = 100


def _ordem_vizinho_mais_proximo(servicos: list[dict], base: tuple[float, float], resolver) -> list[dict]:
    """Semente do sequenciador (18/09): sai da base pro servico mais
    proximo, dali pro mais proximo ainda nao visitado, e assim por
    diante. Servico sem coordenada vai pro FINAL, na ordem original.
    Deterministico: empate resolvido pela ordem original."""
    com, sem = [], []
    for s in servicos:
        (com if resolver(s) else sem).append(s)
    ordem: list[dict] = []
    atual = base
    restantes = list(com)
    while restantes:
        proximo = min(restantes, key=lambda s: _distancia_km(atual[0], atual[1], *resolver(s)))
        restantes.remove(proximo)
        ordem.append(proximo)
        atual = resolver(proximo)
    return ordem + sem


def ordenar_com_janelas(servicos: list[dict], base_lat: float, base_lng: float,
                        api_key: str | None = None, coords_fn=None) -> list[dict]:
    """
    Sequenciamento de UMA rota (usado por otimizacao_rotas.ordenar_2opt,
    que so delega pra ca). Desde 18/09 (Hugo -- "sequencia livre",
    revoga a regra "mais longe primeiro" de 03/08):

      - semente: vizinho mais proximo saindo da base
        (_ordem_vizinho_mais_proximo);
      - objetivo SEM janela: km do trajeto base -> p1 -> ... -> pN, SEM
        volta a base (a rota real termina na ultima entrega);
      - objetivo COM janela (Hugo, 09/09): km + PESO_ATRASO_JANELA_KM x
        horas de atraso + PESO_ESPERA_JANELA_KM x horas de espera
        (simular_horarios);
      - busca local: 2-opt (reversao de segmento) e or-opt (realocacao
        de 1 parada), em QUALQUER posicao -- a 1a parada tambem se move.

    Servico sem coordenada nao entra no calculo de km e a semente o
    deixa no final. No caminho SEM janela, reversao que envolveria essa
    parada e pulada (_tem_coords), entao ela nao sai do lugar. No
    caminho COM janela a busca local PODE reposiciona-la -- o custo
    dela e avaliado pelo simulador de horarios (simular_horarios) e a
    distancia dela conta zero, entao ela pode ser usada pra "absorver"
    espera sem custo de km. Vies conhecido, pre-existente (o ramo com
    janela sempre avaliou custo por _custo/simular_horarios, sem trava
    de coordenada), aceito em 20/09 por ser caso de borda -- desde a
    task de coordenada embutida quase todo servico chega com
    latitude/longitude, embutida ou geocodificada.
    """
    resolver = coords_fn or (lambda s: coords_do_servico(s, api_key))
    base = (base_lat, base_lng)

    ordem_inicial = _ordem_vizinho_mais_proximo(servicos, base, resolver)
    n = len(ordem_inicial)
    com_janela = tem_janela(ordem_inicial)
    if n <= 1 or (n <= 2 and not com_janela):
        return ordem_inicial

    coords = [resolver(s) for s in ordem_inicial]
    coords_por_objeto = {id(s): c for s, c in zip(ordem_inicial, coords)}
    resolver_cache = lambda s: coords_por_objeto.get(id(s), resolver(s))

    def _ponto(rota: list[int], pos: int):
        """Coordenada na posicao `pos`; base antes da 1a parada; None
        depois da ultima (nao ha perna de volta)."""
        if pos < 0:
            return base
        if pos >= len(rota):
            return None
        return coords[rota[pos]]

    def _perna(a, b) -> float:
        return _distancia_km(*a, *b) if (a is not None and b is not None) else 0.0

    def _delta_km(rota: list[int], i: int, j: int) -> float:
        a, b = _ponto(rota, i - 1), _ponto(rota, i)
        c, d = _ponto(rota, j), _ponto(rota, j + 1)
        return (_perna(a, c) + _perna(b, d)) - (_perna(a, b) + _perna(c, d))

    def _tem_coords(rota: list[int], i: int, j: int) -> bool:
        vizinhos = [k for k in (i - 1, j + 1) if 0 <= k < len(rota)]
        return not (any(coords[rota[k]] is None for k in range(i, j + 1))
                    or any(coords[rota[k]] is None for k in vizinhos))

    def _km_total(rota: list[int]) -> float:
        total = 0.0
        anterior = base
        for k in rota:
            c = coords[k]
            if c is None:
                continue
            total += _distancia_km(anterior[0], anterior[1], c[0], c[1])
            anterior = c
        return total

    def _custo(rota: list[int]) -> float:
        sim = simular_horarios([ordem_inicial[k] for k in rota], api_key, base, coords_fn=resolver_cache)
        return (_km_total(rota) + PESO_ATRASO_JANELA_KM * sim["atraso_h"]
                + PESO_ESPERA_JANELA_KM * sim["espera_h"])

    def _2opt_sem_janela(rota: list[int]) -> list[int]:
        melhorou, iteracoes = True, 0
        while melhorou and iteracoes < MAX_ITERACOES_2OPT:
            melhorou = False
            iteracoes += 1
            for i in range(0, len(rota) - 1):
                for j in range(i + 1, len(rota)):
                    if not _tem_coords(rota, i, j):
                        continue
                    if _delta_km(rota, i, j) < -0.01:  # melhoria significativa (> 10m)
                        rota[i:j + 1] = reversed(rota[i:j + 1])
                        melhorou = True
        return rota

    def _busca_local_com_janela(rota: list[int]) -> list[int]:
        custo_atual = _custo(rota)
        melhorou, iteracoes = True, 0
        while melhorou and iteracoes < MAX_ITERACOES_2OPT:
            melhorou = False
            iteracoes += 1
            for i in range(0, len(rota) - 1):
                for j in range(i + 1, len(rota)):
                    candidata = rota[:i] + rota[i:j + 1][::-1] + rota[j + 1:]
                    custo = _custo(candidata)
                    if custo < custo_atual - 0.01:
                        rota, custo_atual, melhorou = candidata, custo, True
            for i in range(0, len(rota)):
                item = rota[i]
                restante = rota[:i] + rota[i + 1:]
                for pos in range(0, len(rota)):
                    if pos == i:
                        continue
                    candidata = restante[:pos] + [item] + restante[pos:]
                    custo = _custo(candidata)
                    if custo < custo_atual - 0.01:
                        rota, custo_atual, melhorou = candidata, custo, True
                        break
                if melhorou:
                    break
        return rota

    rota = list(range(n))
    rota = _busca_local_com_janela(rota) if com_janela else _2opt_sem_janela(rota)
    return [ordem_inicial[k] for k in rota]


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
      - pedido "gigante" (mais caixas que `volume_maximo`) de nível
        1/2/3: sempre isolado;
      - nível 4 sem par possível (nenhum outro nível 4 do MESMO
        endereço e MESMO embarcador, com a mesma condição de
        agendamento -- ver _chave_nivel4): isolado, como sempre foi;
      - nível 4 do MESMO endereço de entrega (`address`) e MESMO
        embarcador (`sender_id`) -- e, quando AMBOS têm agendamento
        (`scheduled_start`), também mesma data -- formam um GRUPO
        (pedido do Hugo, 17/08 e 09/09). Se o volume somado do grupo já
        classifica em algum tipo de veículo grande (regras/tipo_veiculo.
        classificar_tipo_veiculo, 1 endereço), o grupo inteiro sai como
        1 rota desse tipo -- sem teto de `volume_maximo` caixas nem de
        `tamanho_maximo_nivel4` pedidos, "gigante" incluído (pedido do
        Hugo, 09/09; ver _empacotar_grupo_nivel4). Senão, vale a regra
        de última milha de sempre: "gigante" isolado, até
        `tamanho_maximo_nivel4` pedidos e `volume_maximo` caixas por
        rota, respeitando a mesma trava de distância dos demais
        sublotes (Grande SP x Viagem, via `eh_viagem_fn`/
        `distancia_maxima_viagem_km`, igual aos outros modelos deste
        pacote);
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

    def _empacotar_grupo_nivel4(servicos_grupo: list[dict]) -> list[list[dict]]:
        """Grupo de nível 4 do mesmo endereço/embarcador (ver
        _chave_nivel4) -> sublotes. Pedido do Hugo, 09/09:
          1. volume somado classifica em algum tipo de veículo grande
             (1 endereço -> VAN/HR, VUC, 3/4 ou Truck): 1 rota só, sem
             teto de caixas/pedidos de última milha;
          2. senão, tenta o maior "prefixo" (pedidos em ordem de caixas
             DECRESCENTE) que classifica em algum tipo -- ex: 13 pedidos
             de 100cx = 1300cx não cabe em nada (3/4 vai até 1200, Truck
             começa em 1500), mas os 12 primeiros fecham um 3/4 e o
             último segue sozinho pela regra de última milha;
          3. o que sobra sem chegar a veículo grande (ou o grupo inteiro,
             quando nenhum prefixo classifica) segue a regra de sempre:
             "gigante" (> volume_maximo) isolado, demais empacotados por
             _empacotar_grupo (até tamanho_maximo_nivel4 pedidos e
             volume_maximo caixas).
        Mesmo endereço por construção -- a trava de distância entre
        pares não tem o que checar no caso 1/2."""
        restante = sorted(servicos_grupo, key=lambda s: -extrair_volume_caixas(s))
        sublotes_grupo: list[list[dict]] = []
        while restante:
            acumulado = 0
            melhor_prefixo = None
            for i, servico in enumerate(restante):
                acumulado += extrair_volume_caixas(servico)
                if classificar_tipo_veiculo(acumulado, 1) is not None:
                    melhor_prefixo = i
            if melhor_prefixo is None:
                break
            sublotes_grupo.append(restante[:melhor_prefixo + 1])
            restante = restante[melhor_prefixo + 1:]
        if restante:
            gigantes_grupo = [s for s in restante if extrair_volume_caixas(s) > volume_maximo]
            comuns_grupo = [s for s in restante if extrair_volume_caixas(s) <= volume_maximo]
            sublotes_grupo.extend([g] for g in gigantes_grupo)
            if comuns_grupo:
                sublotes_grupo.extend(_empacotar_grupo(comuns_grupo))
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

    def _extrair_grupos_veiculo_grande(candidatos: list[dict]) -> tuple[list[list[dict]], list[dict]]:
        """
        Extrai como rota exclusiva de veículo grande todo ENDEREÇO cujos
        pedidos somam mais que `volume_maximo` (o teto da rota comum, 100
        caixas) -- pedido do Hugo, 22/09.

        Até 22/09 isto era um crescimento guloso que juntava até 4
        endereços vizinhos (2 no Truck) buscando alcançar o volume mínimo
        do tipo. Saiu: juntar endereços diferentes num veículo grande não
        é o que a operação quer, e o mínimo de 150 da VAN/HR deixava
        descoberto o caso real -- 2 pedidos de 60 caixas pro MESMO
        endereço (120) estouravam a rota comum e não alcançavam a VAN/HR,
        saindo em 2 rotas.

        Pedido individual acima do teto nunca chega aqui: já foi isolado
        como "gigante" antes (ver separar_pedidos_exclusivos).

        Endereço acima da maior capacidade do catálogo (2500 cx, Truck)
        é extraído do mesmo jeito, com alerta -- 1 rota sinalizada é
        melhor que dezenas de rotas pequenas silenciosas pro mesmo
        portão. Dividir em várias rotas de Truck é outro projeto.

        Endereço dentro do teto volta pro pool comum (`sobras`).
        """
        extraidos: list[list[dict]] = []
        sobras: list[dict] = []

        for grupo in _agrupar_por_endereco(candidatos):
            if grupo["caixas"] <= volume_maximo:
                sobras.extend(grupo["pedidos"])
                continue
            if classificar_tipo_veiculo(grupo["caixas"], 1) is None:
                endereco = grupo["pedidos"][0].get("address")
                logger.warning(
                    f"[ALERTA_ALOCACAO] Endereço '{endereco}' soma {grupo['caixas']} caixas, "
                    f"acima da maior capacidade do catálogo ({VOLUME_MAXIMO_GERAL_CX}) -- "
                    f"sai como 1 rota exclusiva sem tipo de veículo definido."
                )
            extraidos.append(grupo["pedidos"])

        return extraidos, sobras

    gigantes: list[dict] = []
    grupos_nivel4: dict[tuple[object, object], list[dict]] = {}
    demais: list[dict] = []

    for servico in servicos:
        # Nível 4 ANTES do "gigante": um nível 4 gigante pertence ao
        # grupo do seu endereço/embarcador (pode fechar um veículo
        # grande com os irmãos -- ver _empacotar_grupo_nivel4); isolado
        # só se o grupo não chegar a veículo grande.
        if extrair_nivel_dificuldade(servico) == NIVEL_ROTA_EXCLUSIVA:
            grupos_nivel4.setdefault(_chave_nivel4(servico), []).append(servico)
            continue
        if extrair_volume_caixas(servico) > volume_maximo:
            gigantes.append(servico)
            continue
        demais.append(servico)

    grupos_veiculo_grande, demais = _extrair_grupos_veiculo_grande(demais)

    sublotes_prontos: list[list[dict]] = [[s] for s in gigantes]
    for grupo in grupos_nivel4.values():
        sublotes_prontos.extend(_empacotar_grupo_nivel4(grupo))
    sublotes_prontos.extend(grupos_veiculo_grande)

    return sublotes_prontos, demais


def dividir_em_sublotes(servicos: list[dict], tamanho_minimo: int = 10, tamanho_maximo: int = 18,
                        volume_maximo: int = 100, distancia_maxima_km: float | None = 15,
                        api_key: str | None = None,
                        km_acumulado_maximo: float | None = None) -> list[list[dict]]:
    """
    Divide uma região grande em sublotes respeitando SEIS travas ao
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
        TEMPO_NIVEL3_HORAS e cada nível 1/2 custa
        TEMPO_PARADA_NORMAL_HORAS (calibrados pela execução real em
        25/08, ver bloco de constantes), MAIS o deslocamento estimado:
        perna base -> 1ª parada (quando COORDS_BASE está registrada) e
        pernas entre paradas consecutivas, haversine x FATOR_ESTRADA a
        velocidade urbana/rodovia (22/08 adicionou o deslocamento entre
        paradas; 25/08 a perna da base, o fator estrada e as 2
        velocidades -- antes o orçamento media só tempo de PARADA) --
        ajustado 15/08 e substituído 20/08 (antes: teto FIXO de
        NIVEL_3_TAMANHO_MAXIMO_ROTA pedidos nível 3 por rota; achado
        real, 15/08, que motivou tirar o teto da rota INTEIRA: 24 das
        36 rotas do dia (67%) saíam travadas em 4 por causa disso,
        muitas com só 1 pedido nível-3 "puxando" e descartando o resto
        da vizinhança geográfica fácil; agora o teto por QUANTIDADE
        virou um orçamento por TEMPO -- uma rota com mais nível 3 cabe,
        mas com menos pedidos normais pra compensar; diferente de antes
        de 22/08, uma rota só de nível 1/2 NÃO está mais garantida a
        nunca esbarrar nele -- com deslocamento real embutido, 16
        paradas muito espalhadas geograficamente podem estourar o
        orçamento mesmo sem nenhum nível 3);
      - km ACUMULADO sequencial da rota (soma dos trechos entre itens
        CONSECUTIVOS na ordem de FORMAÇÃO, não a ordem de visita final
        pós-2opt -- ver _km_acumulado_sequencial) até
        `km_acumulado_maximo` (desligado por padrão, `None`). Trava
        NOVA (Fase 1, 22/08): a trava de distância acima é só PAR-A-
        PAR (nenhum par a mais de `distancia_maxima_km`) -- uma
        sequência de saltos de ~18km cada pode passar nela e ainda
        assim virar uma rota de 150km, porque nada soma o trajeto.
        `km_acumulado_maximo` é uma APROXIMAÇÃO (usa a mesma ordem 1D
        de `_chave_ordenacao`, não a ordem vizinho mais próximo +
        2-opt/or-opt sem volta à base real),
        mas já pega o caso zigzag que o par-a-par sozinho não pega.

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

    Nível 4 (pedido do Hugo, 10/08 -- ajustado 15/08, 17/08 e 09/09):
    nunca divide rota com pedido de nível 1/2/3. Mas PODE dividir rota
    com OUTRO nível 4 do MESMO endereço de entrega e MESMO embarcador
    (e mesma data de agendamento, quando ambos têm agendamento); grupo
    que soma volume de veículo grande sai como 1 rota desse tipo -- ver
    _chave_nivel4 e separar_pedidos_exclusivos, chamada abaixo,
    compartilhada por TODOS os esquemas de roteirização (não só este).
    """
    def _chave_ordenacao(servico: dict):
        coords = obter_coordenadas(servico, api_key)
        if coords:
            return (0, coords[0], coords[1])
        cep = extrair_cep(servico)
        return (1, int(cep) if cep else float("inf"), 0.0)

    def _cabe_na_distancia(servico: dict, sublote_atual: list[dict]) -> bool:
        return _cabe_na_distancia_par(servico, sublote_atual, distancia_maxima_km, api_key)

    def _km_incremental(servico: dict, sublote_atual: list[dict]) -> float:
        """Distância do ÚLTIMO item já no sublote (ordem de FORMAÇÃO,
        não a ordem de visita final) até o candidato -- 0.0 se o
        sublote está vazio (nada acumulado ainda) ou sem coordenada de
        um dos lados (não bloqueia, mesmo padrão do resto do módulo).
        Somada incrementalmente em km_acumulado_atual, abaixo -- O(1)
        amortizado por item, sem recalcular o trajeto do zero a cada
        iteração."""
        if not sublote_atual:
            return 0.0
        coords_novo = obter_coordenadas(servico, api_key)
        coords_ultimo = obter_coordenadas(sublote_atual[-1], api_key)
        if not coords_novo or not coords_ultimo:
            return 0.0
        return _distancia_km(*coords_ultimo, *coords_novo)

    sublotes_prontos, demais = separar_pedidos_exclusivos(
        servicos, volume_maximo, distancia_maxima_km, api_key,
    )

    ordenados = sorted(demais, key=_chave_ordenacao)

    sublotes: list[list[dict]] = []
    sublote_atual: list[dict] = []
    caixas_atual = 0
    km_acumulado_atual = 0.0

    for servico in ordenados:
        cx_pedido = extrair_volume_caixas(servico)

        # Recalcula a rota INTEIRA (paradas + deslocamento) com o
        # candidato incluído, em vez de só somar o tempo de parada dele
        # -- assim o trecho de deslocamento até ESSE candidato também
        # entra na conta (estimar_tempo_rota() sozinho não sabe qual
        # seria só o "tempo do pedido"). Exceção: destino já inviável só
        # por distância (ver _orcamento_inviavel_por_distancia) não usa
        # o orçamento pra fragmentar mais -- fragmentar não resolve nada.
        candidato = sublote_atual + [servico]
        cabe_tempo = (estimar_tempo_rota(candidato, api_key) <= ROTA_TEMPO_MAXIMO_HORAS
                     or _orcamento_inviavel_por_distancia(candidato, api_key))
        # Janela de horário (Hugo, 09/09): só custa algo quando algum
        # pedido do candidato tem janela -- ver janela_viavel.
        cabe_janela = janela_viavel(candidato, api_key)

        cabe_entregas = len(sublote_atual) + 1 <= tamanho_maximo
        cabe_caixas = caixas_atual + cx_pedido <= volume_maximo
        cabe_distancia = _cabe_na_distancia(servico, sublote_atual)
        km_ate_aqui = _km_incremental(servico, sublote_atual)
        cabe_km_acumulado = (
            km_acumulado_maximo is None or km_acumulado_atual + km_ate_aqui <= km_acumulado_maximo
        )

        if sublote_atual and not (cabe_entregas and cabe_caixas and cabe_distancia and cabe_tempo
                                  and cabe_km_acumulado and cabe_janela):
            sublotes.append(sublote_atual)
            sublote_atual = []
            caixas_atual = 0
            km_acumulado_atual = 0.0
            km_ate_aqui = 0.0  # 1º item do sublote novo: nenhum antecessor pra somar

        sublote_atual.append(servico)
        caixas_atual += cx_pedido
        km_acumulado_atual += km_ate_aqui

    if sublote_atual:
        sublotes.append(sublote_atual)

    sublotes = fundir_sublotes_pequenos(
        sublotes, tamanho_minimo, tamanho_maximo, volume_maximo, api_key,
        distancia_maxima_km=distancia_maxima_km,
        km_acumulado_maximo=km_acumulado_maximo,
    )

    sublotes.extend(sublotes_prontos)

    return sublotes


def ordenar_por_distancia_base(servicos: list[dict], base_lat: float, base_lng: float,
                               api_key: str | None = None) -> list[dict]:
    """
    Ordena os serviços de uma rota da mais LONGE pra mais PERTO da
    base -- regra de sequenciamento de 03/08, REVOGADA em 18/09 (Hugo:
    "sequência livre", ver ordenar_com_janelas). Sem chamador em
    produção desde então; mantida pra scripts antigos e benchmark.

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
