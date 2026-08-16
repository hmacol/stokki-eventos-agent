# -*- coding: utf-8 -*-
"""
roteirizacao/alocacao_motoristas.py

Classificação de rota (Grande SP x Viagem) e alocação equitativa de
motoristas por rota -- doc de origem:
DOC_EXECUCAO_CLAUDE_ALOCACAO_MOTORISTAS.md.

Reaproveita a infraestrutura já existente do módulo de roteirização em
vez de duplicar:
  - roteirizacao_dados.py: macro_regiao_do_servico (12/08 -- a mesma
    classificação Grande SP x região externa x Viagem que particiona as
    rotas por macro-região), com o mesmo cache de geocodificação já
    usado no resto do agrupamento de rotas.
"""
import logging
from datetime import date

from roteirizacao_dados import macro_regiao_do_servico, MACRO_GRANDE_SP, caixas_e_enderecos
from zonas_sp import classificar_rota_zona
from rodizio_sp import placa_restrita_no_dia, sublote_em_area_rodizio
from regras.preferencias_motoristas import MotoristaPreferencias
from regras.tipo_veiculo import classificar_tipo_veiculo, veiculo_comporta

logger = logging.getLogger(__name__)


def classificar_rota_viagem(sublote: list[dict], api_key: str | None = None) -> bool:
    """
    Retorna True se ao menos 1 entrega do sublote for "Viagem": cidade
    pertencente a uma das regiões EXTERNAS (Vale do Paraíba, Baixada
    Santista, Sorocaba, Campinas, Piracicaba) OU distância > 70km do
    centro de São Paulo -- delega pra macro_regiao_do_servico
    (roteirizacao_dados.py), a mesma classificação que particiona as
    rotas por macro-região. Barueri e ABCD (regiões de dia fixo DENTRO
    da Grande SP, adicionadas em 12/08) NÃO contam como Viagem --
    contavam por efeito colateral entre 12/08 e esta correção. Serviço
    sem cidade/coordenada reconhecível não conta como viagem por falta
    de dado (mesmo padrão seguro do resto do módulo).
    """
    return any(
        macro_regiao_do_servico(servico, api_key) != MACRO_GRANDE_SP
        for servico in sublote
    )


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
      - rodízio de placas de SP (pedido do Hugo, 11/08, ver
        rodizio_sp.py): se a rota tem pelo menos 1 parada dentro do
        Centro Expandido e `data_rota` é dia útil, motorista cuja placa
        está restrita nesse dia NUNCA é considerado pra essa rota --
        trava igualmente rígida, independente de Viagem/Zona. Motorista
        sem PLACA cadastrada não é afetado por essa trava.
      - veículo grande (pedido do Hugo, 15/08, ver regras/tipo_veiculo.py):
        se o sublote classifica como VAN/HR, VUC, 3/4 ou Truck (ver
        classificar_tipo_veiculo, a partir do volume/nº de endereços do
        próprio sublote -- mesma classificação usada no empacotamento,
        roteirizacao_dados.py::separar_pedidos_exclusivos), só motorista
        com TIPO_VEICULO cadastrado de capacidade igual ou maior é
        elegível (veiculo_comporta -- ex: motorista de Truck também
        serve rota classificada VUC). Motorista sem TIPO_VEICULO
        cadastrado nunca é elegível pra essa rota (dado ausente não
        deve virar elegibilidade "universal" pra veículo grande). Rota
        fora da faixa de veículo grande (classificação None) não é
        afetada por essa trava, igual a hoje.

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

    tipo_veiculo = classificar_tipo_veiculo(*caixas_e_enderecos(sublote))
    tipo_veiculo_necessario = tipo_veiculo.codigo if tipo_veiculo else None

    # Rodízio de placas de SP: só vale a pena checar a área do sublote
    # 1 vez (não por motorista) se o dia da semana sequer tem alguma
    # restrição de dígito (segunda-sexta) -- sábado/domingo pula direto.
    rota_em_rodizio = dia_semana in (0, 1, 2, 3, 4) and sublote_em_area_rodizio(sublote, api_key)

    elegiveis = [
        m for m in motoristas
        if m.ativo
        and dia_semana in m.dias_disponiveis
        and contagem_alocacoes_dia.get(m.agent_id, 0) < m.max_rotas_dia
        and (m.aceita_viagens if eh_viagem else True)
        and (zona is None or zona in m.zonas_preferidas)
        and not (rota_em_rodizio and placa_restrita_no_dia(m.placa, dia_semana))
        and veiculo_comporta(m.tipo_veiculo, tipo_veiculo_necessario)
    ]

    if not elegiveis:
        tipo_str = "VIAGEM" if eh_viagem else f"Grande SP/{zona or 'zona desconhecida'}"
        rodizio_str = " [dentro do Centro Expandido -- rodízio pode ter reduzido os elegíveis]" if rota_em_rodizio else ""
        veiculo_str = f" [veículo grande: {tipo_veiculo_necessario}]" if tipo_veiculo_necessario else ""
        logger.warning(
            f"[ALERTA_ALOCACAO] Nenhum motorista elegível para rota tipo [{tipo_str}]{rodizio_str}{veiculo_str} em "
            f"{data_rota.isoformat()} -- rota será criada sem motorista."
        )
        return None

    # Ordenação equitativa: menor número de alocações no dia; empate
    # resolvido por agent_id (round-robin circular estável -- a ordem
    # entre motoristas com a mesma contagem sempre alterna da mesma
    # forma, sem favorecer nenhum deles arbitrariamente).
    elegiveis.sort(key=lambda m: (contagem_alocacoes_dia.get(m.agent_id, 0), m.agent_id))
    return elegiveis[0]
