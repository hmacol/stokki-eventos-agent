# -*- coding: utf-8 -*-
"""
selecao_modelo.py

Seleção diária do melhor modelo de roteirização (pedido do Hugo,
10/08: "para cada dia fazer uma análise e verificar qual seria a
melhor opção para o dia em questão e aí sim liberar as rotas").

Em vez de fixar UM algoritmo de agrupamento, roda os 5 candidatos
(os mesmos esquemas do Laboratório de Roteirização, /laboratorio-rotas)
sobre os pedidos do dia:
  - Atual (Grade+Greedy): agrupar_por_regiao -> consolidar_regioes_
    pequenas -> dividir_em_sublotes (o fluxo de produção de sempre);
  - Sweep Polar (otimizacao_rotas.agrupar_por_sweep);
  - Clarke-Wright Savings (otimizacao_rotas.agrupar_por_savings);
  - CEP real (otimizacao_rotas.agrupar_por_cep);
  - K-means geográfico (otimizacao_rotas.agrupar_por_kmeans);

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

O botão "Roteirizar" da tela de Planejamento (Hugo, 15/08) deixa o
usuário escolher um esquema específico em vez do automático -- ver
parâmetro `modelo_forcado` de `escolher_melhor_modelo`.

Cada decisão é registrada em dados/selecao_modelo_historico.txt
(uma linha por partição por dia) pra auditoria de qual modelo vem
ganhando ao longo do tempo.
"""
import logging
from datetime import date
from pathlib import Path

from roteirizacao_dados import (
    agrupar_por_regiao, consolidar_regioes_pequenas, dividir_em_sublotes,
    calcular_km_estimado, extrair_volume_caixas, particionar_por_macro_regiao,
    caixas_e_enderecos,
)
from otimizacao_rotas import (
    agrupar_por_sweep, agrupar_por_savings, agrupar_por_cep, agrupar_por_kmeans, ordenar_2opt,
)
from alocacao_motoristas import classificar_rota_viagem
from regras.tipo_veiculo import classificar_tipo_veiculo

logger = logging.getLogger(__name__)

ARQUIVO_HISTORICO = Path(__file__).parent / "dados" / "selecao_modelo_historico.txt"


def _km_total(sublotes, base_lat, base_lng, api_key):
    """KM total estimado (haversine) de todas as rotas, somando o km de
    cada sublote individualmente (roteirizacao_dados.calcular_km_estimado)."""
    return sum(calcular_km_estimado(sublote, base_lat, base_lng, api_key) for sublote in sublotes)


def _validar(servicos, sublotes, tamanho_maximo, volume_maximo):
    """Travas + cobertura -- candidato que falhar aqui sai do páreo.

    Sublote classificado como veículo grande (regras/tipo_veiculo.py --
    pedido do Hugo, 15/08) fica de fora das travas de entregas/caixas
    de última milha (as travas que valem pra ele são as do PRÓPRIO
    tipo, já garantidas na hora do empacotamento -- ver
    roteirizacao_dados.py::_extrair_grupos_veiculo_grande)."""
    for sublote in sublotes:
        if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
            continue
        assert len(sublote) <= tamanho_maximo, f"sublote com {len(sublote)} entregas (máx {tamanho_maximo})"
        caixas = sum(extrair_volume_caixas(s) for s in sublote)
        assert caixas <= volume_maximo or len(sublote) == 1, f"sublote com {caixas} caixas (máx {volume_maximo})"
    ids_originais = {s["id"] for s in servicos}
    ids_alocados = [s["id"] for sub in sublotes for s in sub]
    assert len(ids_alocados) == len(set(ids_alocados)), "pedido duplicado entre sublotes"
    assert ids_originais == set(ids_alocados), "pedido perdido no agrupamento"


def agrupar_atual(servicos, gmaps_key, tamanho_minimo, tamanho_maximo,
                  volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km,
                  km_acumulado_maximo=None, km_acumulado_maximo_viagem=None):
    """Agrupamento de produção de sempre, região a região, com o limite
    de distância decidido pelo tipo da região (Grande SP x Viagem) --
    mesma lógica que vivia em criar_rotas_diarias._rotear_particao.

    Função PÚBLICA (sem "_", 15/08): além de ser um dos 5 candidatos de
    escolher_melhor_modelo (via _por_macro, abaixo), também é chamada
    DIRETO por criar_rotas_diarias.py no fluxo de reserva (quando a
    geocodificação da base falha e não dá pra comparar os 5 esquemas --
    esta é a única que não depende de base_lat/base_lng). Antes esse
    fluxo de reserva reimplementava a mesma lógica sem a fusão de
    macro-região nem a consolidação consciente de distância -- agora
    reaproveita esta função e ganha as duas de graça.

    `servicos` já chega filtrado numa ÚNICA macro-região (quem chama é
    sempre _por_macro ou o fluxo de reserva, cada um já particionando
    por macro-região antes) -- então classificar_rota_viagem sobre o
    lote inteiro, uma vez só, já vale pra decidir o teto de distância
    da CONSOLIDAÇÃO (ver distancia_maxima_km de consolidar_regioes_
    pequenas, pedido do Hugo, 15/08: sem isso a fusão por contagem
    perseguia vizinha distante demais, e dividir_em_sublotes quebrava
    de novo por distância -- desperdiçando o esforço)."""
    eh_viagem_lote = classificar_rota_viagem(servicos, gmaps_key)
    distancia_maxima_consolidacao = distancia_maxima_viagem_km if eh_viagem_lote else distancia_maxima_km

    grupos = agrupar_por_regiao(servicos, api_key=gmaps_key)
    grupos = consolidar_regioes_pequenas(grupos, minimo=tamanho_minimo, api_key=gmaps_key,
                                         distancia_maxima_km=distancia_maxima_consolidacao)
    sublotes = []
    for servicos_regiao in grupos.values():
        eh_viagem_regiao = classificar_rota_viagem(servicos_regiao, gmaps_key)
        distancia_regiao = distancia_maxima_viagem_km if eh_viagem_regiao else distancia_maxima_km
        km_acumulado_regiao = km_acumulado_maximo_viagem if eh_viagem_regiao else km_acumulado_maximo
        sublotes.extend(dividir_em_sublotes(
            servicos_regiao, tamanho_minimo=tamanho_minimo, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_regiao, api_key=gmaps_key,
            km_acumulado_maximo=km_acumulado_regiao,
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
                           modelo_forcado: str | None = None,
                           distancia_maxima_fusao_regiao_km: float | None = None,
                           km_acumulado_maximo: float | None = None,
                           km_acumulado_maximo_viagem: float | None = None,
                           ) -> tuple[str, list[list[dict]]]:
    """
    Avalia os 5 agrupamentos sobre os pedidos do dia e retorna
    (nome_do_vencedor, sublotes_já_sequenciados_com_2opt), prontos pra
    virar rotas de verdade. Critério: menos rotas; empate decidido
    pelo menor KM total estimado.

    `modelo_forcado` (Hugo, 15/08 -- escolha manual no botão
    "Roteirizar" de Planejamento): se informado, roda só esse esquema
    em vez de comparar os 5 -- precisa bater com uma das chaves do
    dict `candidatos` abaixo (as mesmas do Laboratório de
    Roteirização), senão levanta ValueError.

    Trava de macro-região (pedido do Hugo, 12/08): os pedidos são
    particionados por macro-região (Grande SP x cada região externa x
    Viagem genérica) ANTES de qualquer modelo rodar, e cada candidato
    agrupa cada partição em separado -- nenhum modelo tem como produzir
    rota misturando Sorocaba com Barueri, por exemplo. A comparação e o
    vencedor continuam GLOBAIS (soma das partições), uma linha de
    histórico por partição de carga, como antes.

    `distancia_maxima_fusao_regiao_km` (Hugo, 15/08): quando informado,
    macro-região com menos que `tamanho_minimo` pedidos funde com a
    OUTRA macro-região mais próxima (centroide real), como último
    recurso, mas só até esse teto de distância -- ver
    roteirizacao_dados.particionar_por_macro_regiao. Sem isso (None,
    padrão): macro-regiões sempre 100% isoladas, como antes.

    `km_acumulado_maximo`/`km_acumulado_maximo_viagem` (Fase 1, 22/08):
    teto de km ACUMULADO sequencial da rota (soma dos trechos, não só
    par-a-par) -- ver roteirizacao_dados.dividir_em_sublotes. Vale só
    pro candidato "Atual (Grade+Greedy)" nesta fase; os outros 4
    esquemas (Sweep/Clarke-Wright/CEP/K-means) não ganham essa trava
    ainda -- mesma assimetria intencional documentada na Fase 0.
    """
    eh_viagem_fn = lambda sub: classificar_rota_viagem(sub, gmaps_key)

    particoes_macro = particionar_por_macro_regiao(
        servicos, gmaps_key, tamanho_minimo=tamanho_minimo,
        distancia_maxima_fusao_km=distancia_maxima_fusao_regiao_km,
    )
    if len(particoes_macro) > 1:
        resumo = ", ".join(f"{macro}: {len(svcs)}" for macro, svcs in sorted(particoes_macro.items()))
        logger.info(f"[{label}] Macro-regiões do dia (roteirizadas em separado) -- {resumo}.")

    def _por_macro(agrupar_uma_particao) -> list[list[dict]]:
        return [sub for svcs in particoes_macro.values() for sub in agrupar_uma_particao(svcs)]

    candidatos = {
        "Atual (Grade+Greedy)": lambda: _por_macro(lambda svcs: agrupar_atual(
            svcs, gmaps_key, tamanho_minimo, tamanho_maximo,
            volume_maximo, distancia_maxima_km, distancia_maxima_viagem_km,
            km_acumulado_maximo, km_acumulado_maximo_viagem)),
        "Sweep Polar": lambda: _por_macro(lambda svcs: agrupar_por_sweep(
            svcs, base_lat, base_lng, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_maxima_km,
            api_key=gmaps_key, distancia_maxima_viagem_km=distancia_maxima_viagem_km,
            eh_viagem_fn=eh_viagem_fn)),
        "Clarke-Wright": lambda: _por_macro(lambda svcs: agrupar_por_savings(
            svcs, base_lat, base_lng, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_maxima_km,
            api_key=gmaps_key, distancia_maxima_viagem_km=distancia_maxima_viagem_km,
            eh_viagem_fn=eh_viagem_fn)),
        "CEP real": lambda: _por_macro(lambda svcs: agrupar_por_cep(
            svcs, base_lat, base_lng, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_maxima_km,
            api_key=gmaps_key, distancia_maxima_viagem_km=distancia_maxima_viagem_km,
            eh_viagem_fn=eh_viagem_fn)),
        "K-means geográfico": lambda: _por_macro(lambda svcs: agrupar_por_kmeans(
            svcs, base_lat, base_lng, tamanho_maximo=tamanho_maximo,
            volume_maximo=volume_maximo, distancia_maxima_km=distancia_maxima_km,
            api_key=gmaps_key, distancia_maxima_viagem_km=distancia_maxima_viagem_km,
            eh_viagem_fn=eh_viagem_fn)),
    }

    if modelo_forcado is not None:
        if modelo_forcado not in candidatos:
            raise ValueError(
                f"Esquema de roteirização desconhecido: {modelo_forcado!r}. "
                f"Opções: {', '.join(candidatos)}."
            )
        candidatos = {modelo_forcado: candidatos[modelo_forcado]}

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
