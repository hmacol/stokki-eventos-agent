# -*- coding: utf-8 -*-
"""
rota_tarde.py

Rota da tarde (spec 2026-10-23, secao 5.4; decisao (b) do Hugo, 23/10):
pedido cuja janela abre a partir de HORA_CORTE_TARDE e que, numa rota da
manha, faria o motorista esperar alem de ESPERA_MAXIMA_ROTA_HORAS (ou que
ficou sozinho numa rota de 1 parada) sai da rota da manha e vai pra uma
rota propria, que sai da base mais tarde (Lalamove ou segunda saida de
motorista, a equipe escolhe).

  - separar_pedidos_tarde: tira esses pedidos das rotas da manha. Roda em
    criar_rotas_diarias.planejar_sublotes depois do polimento e ANTES da
    juncao/adiamento de rotas fracas (Claude, 23/10: senao o pedido seria
    segurado todo dia).
  - hora_saida_tarde: saida = inicio da janela tardia mais cedo menos a ida
    desde a base, arredondada pra baixo em 15 min, nunca antes de
    HORA_SAIDA_BASE (Claude, 23/10).
  - montar_rotas_tarde: forma as rotas da tarde com as travas de sempre
    (paradas, caixas, 9h, janela/espera contadas a partir da saida da
    propria rota).

Escopo (Claude, 23/10): o mesmo recorte de exige_orcamento_horas. Pedido
sem coordenada, nivel 4, de rota de viagem/interior, de veiculo grande ou
inviavel so por distancia nunca sai. ROTA_TARDE_ATIVA=False desliga tudo.
"""
import math

import roteirizacao_dados
from roteirizacao_dados import (
    ESPERA_MAXIMA_ROTA_HORAS, MACRO_GRANDE_SP, NIVEL_ROTA_EXCLUSIVA, ROTA_TEMPO_MAXIMO_HORAS,
    _cabe_na_distancia_par, _distancia_km, _orcamento_inviavel_por_distancia, _tempo_perna_horas,
    eh_janela_tardia, estimar_tempo_rota, exige_orcamento_horas, extrair_janela, extrair_nivel_dificuldade,
    extrair_volume_caixas, janela_respeitada, macro_regiao_do_servico, obter_coordenadas,
    ordenar_com_janelas, simular_horarios,
)
from otimizacao_rotas import ordenar_2opt
from regras.tipo_veiculo import classificar_tipo_veiculo


def _rota_de_viagem(sublote: list[dict], api_key: str | None) -> bool:
    return any(macro_regiao_do_servico(s, api_key) != MACRO_GRANDE_SP for s in sublote)


def _pode_ir_pra_tarde(servico: dict, api_key: str | None) -> bool:
    return (eh_janela_tardia(servico)
            and obter_coordenadas(servico, api_key) is not None
            and extrair_nivel_dificuldade(servico) != NIVEL_ROTA_EXCLUSIVA)


def _parada_sozinha_no_recorte(servico: dict, api_key: str | None, coords_base) -> bool:
    """Rota de 1 parada no recorte de exige_orcamento_horas (que recusa
    qualquer rota de 1 parada): ultima milha comum da Grande SP."""
    return (_pode_ir_pra_tarde(servico, api_key)
            and not _rota_de_viagem([servico], api_key)
            and classificar_tipo_veiculo(extrair_volume_caixas(servico), 1) is None
            and not _orcamento_inviavel_por_distancia([servico], api_key, coords_base))


def _tardio_que_mais_espera(sublote: list[dict], api_key: str | None, coords_base) -> int | None:
    """Indice do pedido que pode ir pra tarde com a maior espera antes da
    janela, na ordem dada. None se nenhum espera."""
    chegadas = simular_horarios(sublote, api_key, coords_base)["chegadas"]
    melhor, maior = None, 0.0
    for i, s in enumerate(sublote):
        if not _pode_ir_pra_tarde(s, api_key):
            continue
        espera = extrair_janela(s)[0] - chegadas[i]
        if espera > maior:
            melhor, maior = i, espera
    return melhor


def separar_pedidos_tarde(sublotes: list[list[dict]], coords_base,
                          api_key: str | None) -> tuple[list[list[dict]], list[dict]]:
    """Tira das rotas da manha os pedidos que vao pra rota da tarde.

    Rota no recorte (exige_orcamento_horas) reprovada pela trava de espera
    tardia: sai, um por vez, o pedido tardio que mais espera, e o resto e
    resequenciado (ordenar_2opt), ate a rota passar em janela_respeitada
    ou a espera tardia caber em ESPERA_MAXIMA_ROTA_HORAS (reprovacao por
    outro motivo nao e assunto daqui) ou nao sobrar tardio. Rota de 1
    parada tardia (inclusive a que sobra disso) vai inteira pra tarde.

    Devolve (sublotes da manha sem os vazios, pedidos da tarde). Com
    ROTA_TARDE_ATIVA=False devolve a entrada intocada e []."""
    if not roteirizacao_dados.ROTA_TARDE_ATIVA:
        return sublotes, []
    base = coords_base or roteirizacao_dados.COORDS_BASE
    manha: list[list[dict]] = []
    tardios: list[dict] = []
    for sublote in sublotes:
        atual = list(sublote)
        if (len(atual) >= 2 and exige_orcamento_horas(atual, api_key, base)
                and not _rota_de_viagem(atual, api_key)):
            while (len(atual) >= 2
                   and not janela_respeitada(atual, api_key, base)
                   and simular_horarios(atual, api_key, base)["espera_tardia_h"] > ESPERA_MAXIMA_ROTA_HORAS):
                idx = _tardio_que_mais_espera(atual, api_key, base)
                if idx is None:
                    break
                tardios.append(atual.pop(idx))
                if base:
                    atual = ordenar_2opt(atual, base[0], base[1], api_key)
        if len(atual) == 1 and _parada_sozinha_no_recorte(atual[0], api_key, base):
            tardios.append(atual.pop())
        if atual:
            manha.append(atual if len(atual) != len(sublote) else sublote)
    return manha, tardios


def hora_saida_tarde(sublote: list[dict], coords_base, api_key: str | None) -> float:
    """Saida da rota da tarde: inicio da janela tardia mais cedo menos a
    ida da base ate a 1a parada tardia (na ordem dada), arredondada pra
    baixo em multiplos de 0,25 h, nunca antes de HORA_SAIDA_BASE."""
    piso = roteirizacao_dados.HORA_SAIDA_BASE
    tardios = [s for s in sublote if eh_janela_tardia(s)]
    if not tardios:
        return piso
    inicio = min(extrair_janela(s)[0] for s in tardios)
    base = coords_base or roteirizacao_dados.COORDS_BASE
    coords = obter_coordenadas(tardios[0], api_key)
    ida = _tempo_perna_horas(_distancia_km(base[0], base[1], *coords)) if base and coords else 0.0
    return max(piso, math.floor((inicio - ida) * 4 + 1e-9) / 4)


def _sequenciar_tarde(sublote: list[dict], base, api_key: str | None) -> tuple[list[dict], float]:
    """Sequencia com a saida da propria rota e recalcula a saida na ordem nova."""
    saida = hora_saida_tarde(sublote, base, api_key)
    ordem = ordenar_com_janelas(sublote, base[0], base[1], api_key, hora_saida=saida) if base else list(sublote)
    return ordem, hora_saida_tarde(ordem, base, api_key)


def montar_rotas_tarde(pedidos: list[dict], coords_base, api_key: str | None, *,
                       tamanho_maximo: int, volume_maximo: int,
                       distancia_maxima_km: float | None = None) -> list[tuple[list[dict], float]]:
    """Rotas da tarde dos pedidos separados por separar_pedidos_tarde.

    Sequencia todos juntos (ordenar_com_janelas com a saida da tarde) e
    quebra guloso nessa ordem: o pedido seguinte entra na rota atual se
    ela continua com no maximo `tamanho_maximo` paradas e `volume_maximo`
    caixas, a no maximo `distancia_maxima_km` de cada parada dela (a
    trava par a par da manha na Grande SP, _cabe_na_distancia_par; None
    = sem trava), dentro de ROTA_TEMPO_MAXIMO_HORAS e passando em
    janela_respeitada com a saida dela (hora_saida_tarde do pedaco) --
    a trava de espera tambem vale aqui (ex.: bar as 13h + cliente as
    18h30 = duas rotas). Cada pedaco e resequenciado com a propria saida.
    Devolve [(sublote, hora_saida)], [] sem pedidos."""
    if not pedidos:
        return []
    base = coords_base or roteirizacao_dados.COORDS_BASE
    em_ordem = sorted(pedidos, key=lambda s: (extrair_janela(s) or (24.0,))[0])
    ordem, _ = _sequenciar_tarde(em_ordem, base, api_key)

    def cabe(rota: list[dict], servico: dict) -> bool:
        rota = rota + [servico]
        return (len(rota) <= tamanho_maximo
                and _cabe_na_distancia_par(servico, rota[:-1], distancia_maxima_km, api_key)
                and sum(extrair_volume_caixas(s) for s in rota) <= volume_maximo
                and estimar_tempo_rota(rota, api_key, base) <= ROTA_TEMPO_MAXIMO_HORAS
                and janela_respeitada(rota, api_key, base, hora_saida=hora_saida_tarde(rota, base, api_key)))

    pedacos: list[list[dict]] = []
    atual: list[dict] = []
    for servico in ordem:
        if atual and not cabe(atual, servico):
            pedacos.append(atual)
            atual = []
        atual.append(servico)
    if atual:
        pedacos.append(atual)
    return [_sequenciar_tarde(p, base, api_key) for p in pedacos]
