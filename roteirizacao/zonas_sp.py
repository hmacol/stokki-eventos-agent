# -*- coding: utf-8 -*-
"""
zonas_sp.py

Classificação de ZONA dentro da Grande São Paulo, usada como trava
rígida de preferência de área na alocação de motoristas (pedido do
Hugo, 10/08) -- igual em rigor à trava de Viagem (regioes_dia_fixo.py/
alocacao_motoristas.py), mas só se aplica a rota que NÃO é Viagem
(motorista de viagem não tem restrição de zona dentro de SP).

Categorias -- as MESMAS que os motoristas já responderam na pesquisa
de preferências (dados/BD_MOTORISTAS.xlsx, coluna ZONAS_PREFERIDAS):
    ZONA NORTE, ZONA SUL, ZONA LESTE, ZONA OESTE, CENTRO,
    GUARULHOS, ABCD, OSASCO - BARUERI - SANTANA DE PARNAÍBA - ALPHAVILLE,
    COTIA - EMBU DAS ARTES - TABOÃO

Classificação em 2 níveis (pedido do Hugo: "com base na
geolocalização"):
  1. Cidade fora do município de São Paulo, mas dentro da Grande SP:
     identificada por NOME da cidade (mesmo padrão de
     regioes_dia_fixo.py::extrair_cidade) -- Guarulhos, ABCD, Osasco/
     Barueri/Santana de Parnaíba, Cotia/Embu/Taboão da Serra.
  2. Dentro do município de São Paulo (ou cidade não mapeada acima):
     por QUADRANTE geográfico a partir do centro de referência (Praça
     da Sé) -- Centro (raio de RAIO_CENTRO_KM), senão Norte/Sul/Leste/
     Oeste conforme a direção dominante (latitude x longitude) do
     deslocamento em relação ao centro. É uma aproximação por
     geolocalização, não os limites oficiais de bairro/distrito do
     município -- suficiente pra decidir alocação de motorista, não
     pra fins cartográficos.

Serviço sem cidade E sem coordenada reconhecível: classificação
indisponível (None) -- quem chama decide como agir (mesmo padrão
seguro do resto do módulo: dado ausente não bloqueia sozinho).
"""
import unicodedata
from collections import Counter

from regioes_dia_fixo import extrair_cidade
from roteirizacao_dados import obter_coordenadas, _distancia_km

# Identificadores CURTOS e sem vírgula/acento (pedido do Hugo, 10/08:
# guardados em ZONAS_PREFERIDAS separados por vírgula -- um nome
# descritivo com vírgula embutida, tipo "ABCD (Santo André, São
# Bernardo...)", quebraria o split por vírgula da planilha).
ZONA_NORTE = "ZONA NORTE"
ZONA_SUL = "ZONA SUL"
ZONA_LESTE = "ZONA LESTE"
ZONA_OESTE = "ZONA OESTE"
CENTRO = "CENTRO"
GUARULHOS = "GUARULHOS"
ABCD = "ABCD"  # Santo André, São Bernardo do Campo, São Caetano do Sul, Diadema
OSASCO_BARUERI_ALPHAVILLE = "OSASCO-BARUERI-ALPHAVILLE"  # Osasco, Barueri, Santana de Parnaíba
COTIA_EMBU_TABOAO = "COTIA-EMBU-TABOAO"  # Cotia, Embu das Artes, Taboão da Serra

# Cidades (fora do município de São Paulo) mapeadas pra cada zona --
# mesmo padrão de índice invertido de regioes_dia_fixo.py::_montar_indice.
_CIDADES_POR_ZONA = {
    GUARULHOS: ["GUARULHOS"],
    ABCD: ["SANTO ANDRE", "SAO BERNARDO DO CAMPO", "SAO CAETANO DO SUL", "DIADEMA"],
    OSASCO_BARUERI_ALPHAVILLE: ["OSASCO", "BARUERI", "SANTANA DE PARNAIBA"],
    COTIA_EMBU_TABOAO: ["COTIA", "EMBU DAS ARTES", "TABOAO DA SERRA"],
}

COORD_CENTRO_SP = (-23.550520, -46.633309)  # Praça da Sé
RAIO_CENTRO_KM = 5.0


def _normalizar_texto(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def _montar_indice_cidades() -> dict[str, str]:
    indice = {}
    for zona, cidades in _CIDADES_POR_ZONA.items():
        for cidade in cidades:
            indice[_normalizar_texto(cidade)] = zona
    return indice


_INDICE_CIDADES_ZONA = _montar_indice_cidades()


def classificar_zona(servico: dict, api_key: str | None = None) -> str | None:
    """
    Classifica UM serviço numa das zonas (ver docstring do módulo).
    Retorna None se não houver cidade nem coordenada reconhecível.
    """
    cidade = extrair_cidade(servico)
    if cidade:
        zona_por_cidade = _INDICE_CIDADES_ZONA.get(_normalizar_texto(cidade))
        if zona_por_cidade:
            return zona_por_cidade

    coords = obter_coordenadas(servico, api_key)
    if not coords:
        return None

    if _distancia_km(*coords, *COORD_CENTRO_SP) <= RAIO_CENTRO_KM:
        return CENTRO

    delta_lat = coords[0] - COORD_CENTRO_SP[0]
    delta_lng = coords[1] - COORD_CENTRO_SP[1]
    if abs(delta_lat) >= abs(delta_lng):
        return ZONA_NORTE if delta_lat > 0 else ZONA_SUL
    return ZONA_LESTE if delta_lng > 0 else ZONA_OESTE


def classificar_rota_zona(sublote: list[dict], api_key: str | None = None) -> str | None:
    """
    Zona representativa de uma rota inteira: a mais FREQUENTE entre as
    entregas do sublote (moda) -- diferente da trava de Viagem (que
    basta 1 entrega pra classificar a rota inteira), porque zona é uma
    característica de ÁREA predominante, não um risco isolado. Entrega
    sem zona reconhecível não conta pra moda; se NENHUMA entrega tiver
    zona reconhecível, retorna None (sem restrição de zona pra essa
    rota -- mesmo padrão seguro do resto do módulo).
    """
    zonas = [z for z in (classificar_zona(s, api_key) for s in sublote) if z]
    if not zonas:
        return None
    return Counter(zonas).most_common(1)[0][0]
