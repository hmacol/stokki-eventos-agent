# -*- coding: utf-8 -*-
"""
selecao_modelo.py

Seleção diária do melhor modelo de roteirização (pedido do Hugo,
10/08: "para cada dia fazer uma análise e verificar qual seria a
melhor opção para o dia em questão e aí sim liberar as rotas").

Em vez de fixar UM algoritmo de agrupamento, roda os 3 candidatos
sobre os pedidos do dia:
  - Atual (Grade+Greedy): agrupar_por_regiao -> consolidar_regioes_
    pequenas -> dividir_em_sublotes (o fluxo de produção de sempre);
  - Sweep Polar (otimizacao_rotas.agrupar_por_sweep);
  - Clarke-Wright Savings (otimizacao_rotas.agrupar_por_savings);

sequencia TODOS com 2-opt (otimizacao_rotas.ordenar_2opt -- parte do
farthest-first de produção e só aceita trocas que reduzem o trajeto,
mantendo a 1ª entrega como a mais distante da base, requisito do
Hugo), valida travas + cobertura de pedidos de cada candidato, e
escolhe o vencedor por:
  1º MENOS rotas (menos motoristas/veículos no dia);
  2º menor KM total estimado (haversine na ordem de visita) como
     desempate.

Candidato que falhar validação ou estourar exceção é DESCARTADO do
páreo (com log de erro) -- o modelo Atual é o piso de segurança: se
só ele sobreviver, o dia sai exatamente como saía antes.

Cada decisão é registrada em dados/selecao_modelo_historico.txt
(uma linha por partição por dia) pra auditoria de qual modelo vem
ganhando ao longo do tempo.
"""
import logging
from datetime import date
from pathlib import Path

from roteirizacao_dados import (
    agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes,
    calcular_km_estimado, extrair_volume_caixas,
)
from otimizacao_rotas import agrupar_por_sweep, agrupar_por_savings, ordenar_2opt
from alocacao_motoristas import classificar_rota_viagem

logger = logging.getLogger(__name__)

ARQUIVO_HISTORICO = Path(__file__).parent / "dados" / "selecao_modelo_historico.txt"


def _km_total(sublotes, base_lat, base_lng, api_key):
    """KM total estimado (haversine) de todas as rotas, somando o km de
    cada sublote individualmente (roteirizacao_dados.calcular_km_estimado)."""
    return sum(calcular_km_estimado(sublote, base_lat, base_lng, api_key) for sublote in sublotes)


def _validar(servicos, sublotes, tamanho_maximo, volume_maximo):
    """Travas + cobertura -- candidato que falhar aqui sai do páreo."""
    for sublote in sublotes:
        assert len(sublote) <= tamanho_maximo, f"sublote com {len(sublote)} entregas (máx {tamanho_maximo})"
        caixas = sum(extrair_volume_caixas(s) for s in sublote)
        assert caixas <= volume_maximo or len(sublote) == 1, f"sublote com {caixas} caixas (máx {volume_maximo})"
    ids_originais = {s["id"] for s in servicos}
    ids_alocados = [s["id"] for sub in sublotes for s in sub]
    assert len(ids_alocados) == len(set(ids_alocados)), "pedido duplicado entre sublotes"
    assert ids_originais == set(ids_alocados), "pedido perdido no agrupamento"


def _agrupar_atual(servicos, gmaps_key, tamanho_minimo, tamanho_maximo,
                   volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km):
    """Agrupamento de produção de sempre, região a região, com o limite
    de distância decidido pelo tipo da região (Grande SP x Viagem) --
    mesma lógica que vivia em criar_rotas_diarias._rotear_particao."""
    grupos = agrupar_por_regiao(servicos, api_key=gmaps_key)
    grupos = consolidar_regioes_pequenas(grupos, minimo=tamanho_minimo, api_key=gmaps_key)
    sublotes = []
    for servicos_regiao in grupos.values():
        distancia_regiao = (
            distancia_maxima_viagem_km if classificar_rota_viagem(servicos_regiao, gmaps_key)
            else distancia_maxima_km
        )
        sublotes.extend(dividir_em_sublotes(
            servicos_regiao, tamanho_minimo=tamanho_minimo, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_regiao, api_key=gmaps_key,
        ))
    return sublotes


def _registrar_historico(data_alvo, label, vencedor, avaliacoes):
    """Uma linha por partição por dia -- auditável depois com um grep."""
    try:
        ARQUIVO_HISTORICO.parent.mkdir(parents=True, exist_ok=True)
        detalhes = "; ".join(
            f"{nome}: {a['rotas']} rota(s), {a['km']:.1f} km" for nome, a in avaliacoes.items()
        )
        with open(ARQUIVO_HISTORICO, "a", encoding="utf-8") as f:
            f.write(f"{data_alvo.isoformat()} | {label} | vencedor: {vencedor} | {detalhes}\n")
    except Exception as e:
        logger.warning(f"Falha ao registrar histórico de seleção (não afeta as rotas): {e}")


def escolher_melhor_modelo(servicos: list[dict], base_lat: float, base_lng: float,
                           gmaps_key: str | None, data_alvo: date, label: str = "",
                           tamanho_minimo: int = 10, tamanho_maximo: int = 18,
                           volume_maximo: int = 100, distancia_maxima_km: float | None = 20,
                           distancia_maxima_viagem_km: float | None = None,
                           ) -> tuple[str, list[list[dict]]]:
    """
    Avalia os 3 agrupamentos sobre os pedidos do dia e retorna
    (nome_do_vencedor, sublotes_já_sequenciados_com_2opt), prontos pra
    virar rotas de verdade. Critério: menos rotas; empate decidido
    pelo menor KM total estimado.
    """
    eh_viagem_fn = lambda sub: classificar_rota_viagem(sub, gmaps_key)

    candidatos = {
        "Atual (Grade+Greedy)": lambda: _agrupar_atual(
            servicos, gmaps_key, tamanho_minimo, tamanho_maximo,
            volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km),
        "Sweep Polar": lambda: agrupar_por_sweep(
            servicos, base_lat, base_lng, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_maxima_km,
            api_key=gmaps_key, distancia_maxima_viagem_km=distancia_maxima_viagem_km,
            eh_viagem_fn=eh_viagem_fn),
        "Clarke-Wright": lambda: agrupar_por_savings(
            servicos, base_lat, base_lng, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_maxima_km,
            api_key=gmaps_key, distancia_maxima_viagem_km=distancia_maxima_viagem_km,
            eh_viagem_fn=eh_viagem_fn),
    }

    avaliacoes: dict[str, dict] = {}
    for nome, fn in candidatos.items():
        try:
            sublotes = fn()
            sublotes = [ordenar_2opt(s, base_lat, base_lng, gmaps_key) for s in sublotes]
            _validar(servicos, sublotes, tamanho_maximo, volume_maximo)
            avaliacoes[nome] = {
                "sublotes": sublotes,
                "rotas": len(sublotes),
                "km": _km_total(sublotes, base_lat, base_lng, gmaps_key),
            }
        except Exception as e:
            logger.error(f"[{label}] Modelo '{nome}' descartado da seleção do dia: {e}")

    if not avaliacoes:
        # Nem o Atual sobreviveu -- algo muito errado; propaga pra quem
        # chama tratar como falha de roteirização mesmo.
        raise RuntimeError("Nenhum modelo de roteirização produziu agrupamento válido.")

    vencedor = min(avaliacoes, key=lambda nome: (avaliacoes[nome]["rotas"], avaliacoes[nome]["km"]))

    placar = " | ".join(
        f"{nome}: {a['rotas']} rota(s), {a['km']:.1f} km" for nome, a in avaliacoes.items()
    )
    logger.info(f"[{label}] Seleção do dia -- {placar}.")
    logger.info(f"[{label}] Modelo VENCEDOR: {vencedor} "
                f"({avaliacoes[vencedor]['rotas']} rota(s), {avaliacoes[vencedor]['km']:.1f} km).")
    _registrar_historico(data_alvo, label or "-", vencedor, avaliacoes)

    return vencedor, avaliacoes[vencedor]["sublotes"]
