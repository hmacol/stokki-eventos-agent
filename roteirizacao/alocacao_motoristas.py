# -*- coding: utf-8 -*-
"""
roteirizacao/alocacao_motoristas.py

Classificação de rota (Grande SP x Viagem) e alocação equitativa de
motoristas por rota -- doc de origem:
DOC_EXECUCAO_CLAUDE_ALOCACAO_MOTORISTAS.md.

Reaproveita a infraestrutura já existente do módulo de roteirização em
vez de duplicar:
  - regioes_dia_fixo.py: RAIO_GRANDE_SP_KM (70km) e a lista de cidades
    das regiões externas (Vale do Paraíba, Baixada Santista, Sorocaba,
    Campinas, Piracicaba) já cadastradas ali como regiões de dia fixo
    -- são exatamente as "regiões externas" citadas na especificação.
  - roteirizacao_dados.py: obter_coordenadas/_distancia_km, com o mesmo
    cache de geocodificação já usado no resto do agrupamento de rotas.
"""
import logging
from datetime import date

from regioes_dia_fixo import RAIO_GRANDE_SP_KM, extrair_cidade, regiao_da_cidade
from roteirizacao_dados import obter_coordenadas, _distancia_km
from zonas_sp import classificar_rota_zona
from regras.preferencias_motoristas import MotoristaPreferencias

logger = logging.getLogger(__name__)

# Coordenada de referência de São Paulo (centro), mesma usada como base
# do raio da Grande SP em regioes_dia_fixo.py (ENDERECO_REFERENCIA_SP).
COORD_BASE_SP = (-23.550520, -46.633309)


def classificar_rota_viagem(sublote: list[dict], api_key: str | None = None) -> bool:
    """
    Retorna True se ao menos 1 entrega do sublote for "Viagem": cidade
    pertencente a uma das regiões externas (Vale do Paraíba, Baixada
    Santista, Sorocaba, Campinas, Piracicaba) OU distância > 70km do
    centro de São Paulo. Serviço sem cidade/coordenada reconhecível não
    conta como viagem por falta de dado (mesmo padrão seguro do resto
    do módulo -- não bloqueia por dado ausente).
    """
    for servico in sublote:
        cidade = extrair_cidade(servico)
        if cidade and regiao_da_cidade(cidade):
            return True

        coords = obter_coordenadas(servico, api_key)
        if coords and _distancia_km(*COORD_BASE_SP, *coords) > RAIO_GRANDE_SP_KM:
            return True

    return False


def selecionar_motorista_equitativo(
    sublote: list[dict],
    data_rota: date,
    motoristas: list[MotoristaPreferencias],
    contagem_alocacoes_dia: dict[int, int],
    api_key: str | None = None,
) -> "MotoristaPreferencias | None":
    """
    Seleciona o motorista elegível com a menor carga do dia (Least-
    Allocated Load Balancing) -- sem preferência entre motoristas.

    Filtro de elegibilidade: ativo, disponível no dia da semana de
    `data_rota`, com espaço em MAX_ROTAS_DIA, e:
      - se a rota for Viagem -- exige ACEITA_VIAGENS (motorista sem
        essa preferência NUNCA é considerado, mesmo sem nenhum outro
        elegível). Zona não importa pra rota de Viagem.
      - se a rota NÃO for Viagem -- exige que a ZONA predominante da
        rota (ver zonas_sp.py::classificar_rota_zona) esteja entre as
        ZONAS_PREFERIDAS do motorista, trava igualmente rígida (pedido
        do Hugo, 10/08). Rota sem zona reconhecível (dado insuficiente)
        não aplica essa trava -- mesmo padrão seguro do resto do módulo.

    `contagem_alocacoes_dia` é lida mas NÃO é alterada aqui -- quem
    chama incrementa depois de confirmar que a rota foi criada de
    verdade (evita contar uma alocação que falhou na API).

    Retorna None (com [ALERTA_ALOCACAO] no log) se não houver
    motorista elegível -- quem chama decide como seguir (rota sem
    motorista).
    """
    eh_viagem = classificar_rota_viagem(sublote, api_key)
    zona = None if eh_viagem else classificar_rota_zona(sublote, api_key)
    dia_semana = data_rota.weekday()

    elegiveis = [
        m for m in motoristas
        if m.ativo
        and dia_semana in m.dias_disponiveis
        and contagem_alocacoes_dia.get(m.agent_id, 0) < m.max_rotas_dia
        and (m.aceita_viagens if eh_viagem else True)
        and (zona is None or zona in m.zonas_preferidas)
    ]

    if not elegiveis:
        tipo_str = "VIAGEM" if eh_viagem else f"Grande SP/{zona or 'zona desconhecida'}"
        logger.warning(
            f"[ALERTA_ALOCACAO] Nenhum motorista elegível para rota tipo [{tipo_str}] em "
            f"{data_rota.isoformat()} -- rota será criada sem motorista."
        )
        return None

    # Ordenação equitativa: menor número de alocações no dia; empate
    # resolvido por agent_id (round-robin circular estável -- a ordem
    # entre motoristas com a mesma contagem sempre alterna da mesma
    # forma, sem favorecer nenhum deles arbitrariamente).
    elegiveis.sort(key=lambda m: (contagem_alocacoes_dia.get(m.agent_id, 0), m.agent_id))
    return elegiveis[0]
