# -*- coding: utf-8 -*-
"""
roteirizacao/rodizio_sp.py

Rodízio municipal de veículos de São Paulo (Decreto 37.085/1997) --
usado como trava adicional na alocação de motoristas (pedido do Hugo,
11/08): "considerar a placa de cada motorista para verificar os dias
de rodízio de placas em São Paulo... somente regiões que não tenham
rodízio e os motoristas que tem disponibilidade, para as viagens".

Regras oficiais do rodízio municipal:
  - Área: Centro Expandido de SP (perímetro do Minianel Viário --
    Marginal Tietê, Marginal Pinheiros, Av. Bandeirantes/Afonso
    D'Escragnolle Taunay, Av. Salim Farah Maluf). Fora dessa área NÃO
    há restrição nenhuma -- qualquer placa pode circular.
  - Horário: 7h às 10h e das 17h às 20h, em dias úteis (segunda a
    sexta). Sábado, domingo e feriado: sem restrição.
  - Dígito final da placa restrito por dia:
        segunda: 1, 2   terça: 3, 4   quarta: 5, 6
        quinta: 7, 8    sexta: 9, 0

Limite de dado conhecido (documentado, não escondido): o pipeline de
roteirização não calcula horário estimado de chegada por parada -- só
existe um horário de SAÍDA fixo da rota (`start_at`, ver
criar_rotas_diarias.py), sem ETA por serviço. Não é possível saber com
precisão se uma entrega específica cai dentro da janela 7h-10h/17h-20h.
Por isso a trava aqui é por DIA, não por horário fino: rota com pelo
menos 1 parada dentro do Centro Expandido, num dia útil em que o
dígito final da placa do motorista está restrito, torna esse motorista
inelegível para a rota inteira -- mesma "trava rígida" de comportamento
das demais (Viagem, Zona -- ver alocacao_motoristas.py), errando pro
lado seguro (evitar risco de multa) em vez de tentar adivinhar o
horário de chegada.

Área do Centro Expandido: polígono carregado de
dados/centro_expandido_sp.json (GeoJSON com as coordenadas oficiais --
exportar a camada "Restrição à circulação de veículos > MIAN" do
GeoSampa, https://geosampa.prefeitura.sp.gov.br/). SEM esse arquivo,
`em_area_rodizio` sempre retorna False (nenhuma rota é tratada como
dentro da área) e um aviso único é logado -- mesmo padrão seguro do
resto do módulo: dado ausente não bloqueia a alocação, só deixa a
trava de rodízio desligada até o arquivo existir.
"""
import json
import logging
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

CAMINHO_POLIGONO = Path(__file__).resolve().parent.parent / "dados" / "centro_expandido_sp.json"

HORARIOS_RESTRICAO = [(7, 10), (17, 20)]  # (hora_inicio, hora_fim), 24h -- ver limite de dado no docstring do módulo

# dia da semana (date.weekday(): segunda=0 ... sexta=4) -> dígitos finais restritos
DIGITOS_RESTRITOS_POR_DIA = {
    0: {"1", "2"},
    1: {"3", "4"},
    2: {"5", "6"},
    3: {"7", "8"},
    4: {"9", "0"},
}

_aviso_poligono_ausente_emitido = False
_poligono_cache: list | None = None
_poligono_carregado = False


def _carregar_poligono() -> list[tuple[float, float]] | None:
    """Carrega (e cacheia em memória) o polígono do Centro Expandido de
    dados/centro_expandido_sp.json. Formato aceito: GeoJSON (Feature ou
    Polygon puro, usa o anel externo de 'coordinates') OU lista simples
    de pontos [[lat, lon], ...] -- aceita [lat, lon] ou [lon, lat] em
    cada ponto (detecta pela faixa de valor: latitude de SP ~ -24 a
    -23, longitude ~ -47 a -46), pra facilitar colar a exportação
    direta do GeoSampa sem se preocupar com a ordem dos eixos. Retorna
    None (com aviso ÚNICO no log) se o arquivo não existir ou não
    puder ser interpretado."""
    global _aviso_poligono_ausente_emitido, _poligono_cache, _poligono_carregado
    if _poligono_carregado:
        return _poligono_cache

    if not CAMINHO_POLIGONO.exists():
        if not _aviso_poligono_ausente_emitido:
            logger.warning(
                f"[RODIZIO_SP] {CAMINHO_POLIGONO} não encontrado -- trava de rodízio de "
                f"placas DESLIGADA (nenhuma rota é tratada como dentro do Centro Expandido) "
                f"até o arquivo existir."
            )
            _aviso_poligono_ausente_emitido = True
        _poligono_carregado = True
        _poligono_cache = None
        return None

    try:
        bruto = json.loads(CAMINHO_POLIGONO.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"[RODIZIO_SP] Falha ao ler {CAMINHO_POLIGONO}: {e} -- trava de rodízio desligada.")
        _poligono_carregado = True
        _poligono_cache = None
        return None

    coords_brutas = None
    if isinstance(bruto, dict):
        # GeoJSON Feature ou Polygon puro -- pega o anel externo (primeiro nível de coordinates)
        geometria = bruto.get("geometry", bruto)
        coords = geometria.get("coordinates")
        if coords:
            coords_brutas = coords[0]
    elif isinstance(bruto, list):
        coords_brutas = bruto

    if not coords_brutas:
        logger.error(f"[RODIZIO_SP] {CAMINHO_POLIGONO} não tem coordenadas reconhecíveis -- trava de rodízio desligada.")
        _poligono_carregado = True
        _poligono_cache = None
        return None

    poligono = []
    for ponto in coords_brutas:
        a, b = float(ponto[0]), float(ponto[1])
        if -25 < a < -22:  # a já é latitude
            lat, lon = a, b
        else:  # a é longitude (padrão GeoJSON [lon, lat]) -- inverte
            lat, lon = b, a
        poligono.append((lat, lon))

    logger.info(f"[RODIZIO_SP] Polígono do Centro Expandido carregado: {len(poligono)} ponto(s).")
    _poligono_carregado = True
    _poligono_cache = poligono
    return poligono


def em_area_rodizio(lat: float, lon: float) -> bool:
    """Ray casting -- True se (lat, lon) está dentro do polígono do
    Centro Expandido. False se o polígono ainda não está configurado
    (ver _carregar_poligono) ou se o ponto está fora dele."""
    poligono = _carregar_poligono()
    if not poligono:
        return False

    dentro = False
    n = len(poligono)
    j = n - 1
    for i in range(n):
        lat_i, lon_i = poligono[i]
        lat_j, lon_j = poligono[j]
        if (lon_i > lon) != (lon_j > lon):
            lon_j_menos_lon_i = lon_j - lon_i
            if lon_j_menos_lon_i and lat < (lat_j - lat_i) * (lon - lon_i) / lon_j_menos_lon_i + lat_i:
                dentro = not dentro
        j = i
    return dentro


def placa_restrita_no_dia(placa: str | None, dia_semana: int) -> bool:
    """True se o dígito final da `placa` está na lista de restrição do
    dia (0=segunda...6=domingo). Fim de semana (5,6) e placa vazia/
    inválida: nunca restrita (fail-safe -- sem dado, sem bloqueio)."""
    digitos_restritos = DIGITOS_RESTRITOS_POR_DIA.get(dia_semana)
    if not digitos_restritos:
        return False  # sábado/domingo -- sem rodízio

    placa_limpa = "".join(c for c in str(placa or "") if c.isalnum())
    if not placa_limpa:
        return False
    return placa_limpa[-1] in digitos_restritos


def sublote_em_area_rodizio(sublote: list[dict], api_key: str | None = None) -> bool:
    """True se pelo menos 1 serviço do `sublote` tem coordenada dentro
    do Centro Expandido. Serviço sem coordenada reconhecível não conta
    (mesmo padrão seguro do resto do módulo)."""
    from roteirizacao_dados import obter_coordenadas

    for servico in sublote:
        coords = obter_coordenadas(servico, api_key)
        if coords and em_area_rodizio(*coords):
            return True
    return False


def rota_bloqueada_por_rodizio(placa: str | None, sublote: list[dict], data_rota: date,
                                api_key: str | None = None) -> bool:
    """True se o motorista com essa `placa` NÃO pode atender essa rota:
    (a) `data_rota` é dia útil com restrição pra esse dígito final, E
    (b) pelo menos 1 parada do sublote cai dentro do Centro Expandido.
    Motorista sem placa cadastrada: nunca bloqueado por rodízio (dado
    ausente não bloqueia -- fica a cargo de ATIVO/ZONAS/etc. filtrar
    esse motorista se for o caso)."""
    if not placa:
        return False
    if not placa_restrita_no_dia(placa, data_rota.weekday()):
        return False
    return sublote_em_area_rodizio(sublote, api_key)
