# -*- coding: utf-8 -*-
"""
regras/tipo_veiculo.py

Categorias de veículo maior que a última milha padrão (VAN/HR, VUC, 3/4,
Truck), usadas pra decidir quando um GRUPO de pedidos (até 4 endereços
diferentes -- ou até 2 no caso do Truck; mesmo endereço não conta contra
esse limite) já justifica sair da roteirização comum (16 paradas/100
caixas, ver criar_rotas_diarias.py) e virar 1 rota exclusiva de veículo
maior (pedido do Hugo, 15/08).

Peso máximo (kg) fica documentado aqui mas NÃO é aplicado como trava na
roteirização ainda -- o peso de um pedido só é conhecido depois que a
rota já foi criada (extraído da DANFE em gerar_pdf_romaneios.py), não
no momento do agrupamento.

Classificação SEMPRE recalculável a partir do sublote final (caixas +
nº de endereços distintos) -- mesmo padrão de extrair_nivel_dificuldade/
extrair_volume_caixas em roteirizacao_dados.py, sem estado extra
passado entre as etapas do agrupamento.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class TipoVeiculo:
    codigo: str                  # identificador estável (planilha de motoristas, tags de rota)
    nome: str                    # nome de exibição
    peso_maximo_kg: int          # documentado, não aplicado na roteirização ainda
    volume_maximo_cx: int
    volume_minimo_cx: int
    max_enderecos_distintos: int
    # FIORINO é o único False: ele é o veículo da rota comum (100 cx é o
    # mesmo teto de VOLUME_MAXIMO_ROTA), não um veículo que justifica
    # sair da roteirização normal. Ver TIPOS_VEICULO_EXCLUSIVOS abaixo.
    gera_rota_exclusiva: bool = True


# Ordenado por capacidade CRESCENTE -- classificar_tipo_veiculo devolve o
# primeiro que servir (o menor/mais barato que comporta o lote).
#
# Faixas CONTÍGUAS desde 22/09/2026 (Hugo): o volume_minimo_cx virou o
# teto do tipo anterior. Antes havia um buraco -- 101 a 149 caixas num
# endereço estourava a rota comum (100) e não alcançava o mínimo da
# VAN/HR (150), ficando sem veículo nenhum.
TIPOS_VEICULO = [
    TipoVeiculo("FIORINO", "Fiorino", peso_maximo_kg=650, volume_maximo_cx=100,
                volume_minimo_cx=0, max_enderecos_distintos=4,
                gera_rota_exclusiva=False),
    TipoVeiculo("VAN_HR", "VAN/HR", peso_maximo_kg=1300, volume_maximo_cx=400,
                volume_minimo_cx=101, max_enderecos_distintos=4),
    TipoVeiculo("VUC", "VUC", peso_maximo_kg=2000, volume_maximo_cx=600,
                volume_minimo_cx=401, max_enderecos_distintos=4),
    TipoVeiculo("TRES_QUARTOS", "3/4", peso_maximo_kg=6000, volume_maximo_cx=1200,
                volume_minimo_cx=601, max_enderecos_distintos=4),
    TipoVeiculo("TRUCK", "Truck", peso_maximo_kg=10000, volume_maximo_cx=2500,
                volume_minimo_cx=1201, max_enderecos_distintos=2),
]

# Tipos que JUSTIFICAM uma rota exclusiva de veículo grande. FIORINO
# fica de fora: classificar_tipo_veiculo devolver FIORINO faria toda
# rota comum (<=100 cx) virar "rota exclusiva" nos ~12 pontos do
# pipeline que testam `classificar_tipo_veiculo(...) is not None`.
TIPOS_VEICULO_EXCLUSIVOS = [t for t in TIPOS_VEICULO if t.gera_rota_exclusiva]

_TIPOS_POR_CODIGO = {t.codigo: t for t in TIPOS_VEICULO}

# Apelidos pro preenchimento manual na planilha de motoristas (coluna
# TIPO_VEICULO) -- "3/4" é a forma que o Hugo realmente usa (não
# "TRES_QUARTOS"); depois de normalizado (maiúsculo, não-alfanumérico
# vira '_', ver regras/preferencias_motoristas.py::_construir_motorista)
# "3/4" chega aqui como "3_4". "UTILITARIO" é como a planilha antiga
# chamava o Fiorino (ver regras/tarifa_motorista.py, mesma tarifa).
_APELIDOS_CODIGO = {
    "3_4": "TRES_QUARTOS", "34": "TRES_QUARTOS",
    "FIO": "FIORINO", "UTILITARIO": "FIORINO",
}

# Maior teto de caixas entre os tipos que geram rota EXCLUSIVA -- usado
# como limite inicial ao tentar crescer um cluster de endereços em
# roteirizacao_dados.py.
VOLUME_MAXIMO_GERAL_CX = max(t.volume_maximo_cx for t in TIPOS_VEICULO_EXCLUSIVOS)


def tipo_por_codigo(codigo: str | None) -> TipoVeiculo | None:
    """TipoVeiculo correspondente a `codigo`, ou None se vazio/não
    reconhecido -- nunca levanta exceção (mesmo padrão fail-safe do
    resto do módulo de regras)."""
    if not codigo:
        return None
    codigo_normalizado = str(codigo).strip().upper()
    codigo_normalizado = _APELIDOS_CODIGO.get(codigo_normalizado, codigo_normalizado)
    return _TIPOS_POR_CODIGO.get(codigo_normalizado)


def teto_caixas_para_enderecos(qtd_enderecos: int) -> int:
    """Maior `volume_maximo_cx` entre os tipos cujo `max_enderecos_distintos`
    ainda comporta `qtd_enderecos` -- usado durante o empacotamento pra
    saber até quanto ainda vale a pena crescer um cluster (ex: com 2
    endereços o teto é o do Truck, 2500; ao passar pra 3, cai pro maior
    tipo de 4 endereços, 1200, já que Truck deixou de ser alcançável).
    0 se nenhum tipo comportar mais esse tanto de endereços (cluster já
    deve parar de crescer)."""
    candidatos = [t.volume_maximo_cx for t in TIPOS_VEICULO_EXCLUSIVOS if t.max_enderecos_distintos >= qtd_enderecos]
    return max(candidatos) if candidatos else 0


def classificar_tipo_veiculo(caixas: int, enderecos_distintos: int) -> TipoVeiculo | None:
    """
    Menor tipo de veículo EXCLUSIVO (capacidade crescente, sem contar
    FIORINO) cujo `max_enderecos_distintos` comporta `enderecos_distintos`
    E cujo [volume_minimo_cx, volume_maximo_cx] contém `caixas`. None
    quando não cabe em nenhum tipo -- lote fica de fora da faixa de
    veículo grande, segue a roteirização comum (última milha) sem trava
    nenhuma daqui.

    NUNCA devolve FIORINO -- ele é o veículo da própria rota comum (não
    um veículo que justifique rota exclusiva), então fica fora de
    TIPOS_VEICULO_EXCLUSIVOS. Se vazasse, toda rota comum (<=100 cx)
    passaria a ser tratada como "rota exclusiva de veículo grande" nos
    ~12 pontos do pipeline que checam esse retorno.
    """
    for tipo in TIPOS_VEICULO_EXCLUSIVOS:
        if (enderecos_distintos <= tipo.max_enderecos_distintos
                and tipo.volume_minimo_cx <= caixas <= tipo.volume_maximo_cx):
            return tipo
    return None


def veiculo_comporta(tipo_motorista: str | None, tipo_necessario: str | None) -> bool:
    """
    True se um motorista com veículo `tipo_motorista` pode atender uma
    rota que precisa de `tipo_necessario`.

    Regra (Hugo, 25/09): quem não é Fiorino só pega rota do porte EXATO
    do carro -- VAN/HR só rota VAN_HR, VUC só rota VUC, etc. Nem rota
    comum (última milha), nem rota de outro porte, mesmo menor (até
    25/09 valia "capacidade maior ou igual comporta"; a ideia agora é
    reservar o veículo grande pra carga do tamanho dele).

    `tipo_necessario` None (rota comum, fora da faixa de veículo grande)
    é True só pra Fiorino e pra quem está sem tipo. `tipo_motorista`
    None só serve rota comum (dado ausente não deve virar elegibilidade
    universal pra veículo grande). Código não reconhecido em qualquer um
    dos dois é tratado como ausente (mesmo padrão fail-safe do resto do
    módulo).
    """
    if tipo_necessario not in _TIPOS_POR_CODIGO:
        tipo_necessario = None
    if tipo_motorista not in _TIPOS_POR_CODIGO or tipo_motorista == "FIORINO":
        return tipo_necessario is None
    return tipo_motorista == tipo_necessario
