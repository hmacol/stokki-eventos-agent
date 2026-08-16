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


# Ordenado por capacidade CRESCENTE -- classificar_tipo_veiculo devolve o
# primeiro que servir (o menor/mais barato que comporta o lote), e
# veiculo_comporta usa essa mesma ordem pra saber se o veículo de um
# motorista "cobre pra cima" (ex: motorista de Truck também serve rota
# classificada VUC).
TIPOS_VEICULO = [
    TipoVeiculo("VAN_HR", "VAN/HR", peso_maximo_kg=1300, volume_maximo_cx=400,
                volume_minimo_cx=150, max_enderecos_distintos=4),
    TipoVeiculo("VUC", "VUC", peso_maximo_kg=2000, volume_maximo_cx=600,
                volume_minimo_cx=300, max_enderecos_distintos=4),
    TipoVeiculo("TRES_QUARTOS", "3/4", peso_maximo_kg=6000, volume_maximo_cx=1200,
                volume_minimo_cx=500, max_enderecos_distintos=4),
    TipoVeiculo("TRUCK", "Truck", peso_maximo_kg=10000, volume_maximo_cx=2500,
                volume_minimo_cx=1500, max_enderecos_distintos=2),
]

_TIPOS_POR_CODIGO = {t.codigo: t for t in TIPOS_VEICULO}
_ORDEM_CODIGO = {t.codigo: i for i, t in enumerate(TIPOS_VEICULO)}

# Apelidos pro preenchimento manual na planilha de motoristas (coluna
# TIPO_VEICULO) -- "3/4" é a forma que o Hugo realmente usa (não
# "TRES_QUARTOS"); depois de normalizado (maiúsculo, não-alfanumérico
# vira '_', ver regras/preferencias_motoristas.py::_construir_motorista)
# "3/4" chega aqui como "3_4".
_APELIDOS_CODIGO = {"3_4": "TRES_QUARTOS", "34": "TRES_QUARTOS"}

# Maior teto de caixas entre TODOS os tipos -- usado como limite inicial
# ao tentar crescer um cluster de endereços em roteirizacao_dados.py,
# antes de saber quantos endereços o cluster final vai ter.
VOLUME_MAXIMO_GERAL_CX = max(t.volume_maximo_cx for t in TIPOS_VEICULO)


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
    candidatos = [t.volume_maximo_cx for t in TIPOS_VEICULO if t.max_enderecos_distintos >= qtd_enderecos]
    return max(candidatos) if candidatos else 0


def classificar_tipo_veiculo(caixas: int, enderecos_distintos: int) -> TipoVeiculo | None:
    """
    Menor tipo de veículo (capacidade crescente) cujo `max_enderecos_distintos`
    comporta `enderecos_distintos` E cujo [volume_minimo_cx, volume_maximo_cx]
    contém `caixas`. None quando não cabe em nenhum tipo -- lote fica de
    fora da faixa de veículo grande, segue a roteirização comum (última
    milha) sem trava nenhuma daqui.
    """
    for tipo in TIPOS_VEICULO:
        if (enderecos_distintos <= tipo.max_enderecos_distintos
                and tipo.volume_minimo_cx <= caixas <= tipo.volume_maximo_cx):
            return tipo
    return None


def veiculo_comporta(tipo_motorista: str | None, tipo_necessario: str | None) -> bool:
    """
    True se um motorista com veículo `tipo_motorista` pode atender uma
    rota que precisa de `tipo_necessario` -- veículo de capacidade MAIOR
    OU IGUAL comporta (ex: motorista de Truck também serve rota
    classificada VUC/3-4/VAN-HR).

    `tipo_necessario` None (rota comum, fora da faixa de veículo grande)
    sempre True -- essa trava não se aplica a rotas de última milha.
    `tipo_motorista` None só serve rota sem `tipo_necessario` (dado
    ausente não deve virar elegibilidade universal pra veículo grande).
    Código não reconhecido em qualquer um dos dois é tratado como
    ausente (mesmo padrão fail-safe do resto do módulo).
    """
    if tipo_necessario is None or tipo_necessario not in _ORDEM_CODIGO:
        return True
    if tipo_motorista is None or tipo_motorista not in _ORDEM_CODIGO:
        return False
    return _ORDEM_CODIGO[tipo_motorista] >= _ORDEM_CODIGO[tipo_necessario]
