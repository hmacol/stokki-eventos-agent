# -*- coding: utf-8 -*-
"""
otimizacao_rotas.py

Modelos de otimização de roteirização (doc DOC_EXECUCAO_CLAUDE_OTIMIZACAO_ROTAS.md):
  - Modelo 1: Sweep Polar (agrupar_por_sweep)
  - Modelo 2: Clarke-Wright Savings (agrupar_por_savings)
  - Modelo 3: Sequenciamento 2-Opt (ordenar_2opt)
  - Modelo 4: CEP real (agrupar_por_cep) -- pedido do Hugo, 14/08, ver
    laboratório de roteirização (painel_agentes/laboratorio_rotas.py)
  - Modelo 5: Clustering geográfico K-means (agrupar_por_kmeans) --
    mesmo pedido, 14/08

Módulo ISOLADO da produção: roteirizacao_dados.py / criar_rotas_diarias.py
não são alterados -- os modelos daqui rodam lado a lado no benchmark
(benchmark_modelos.py) e a eventual substituição em produção é decidida
depois, com rollback trivial.

Reaproveita as funções utilitárias do ecossistema existente
(roteirizacao_dados.py) -- nunca duplica geocodificação, cache ou
cálculo de distância.

Todos os modelos respeitam as MESMAS travas do dividir_em_sublotes de
produção, inclusive o ORÇAMENTO DE HORAS (roteirizacao_dados.
estimar_tempo_rota <= ROTA_TEMPO_MAXIMO_HORAS -- paridade adicionada em
25/08: até então só o modelo Atual tinha essa trava, e a seleção diária
por "menos rotas" premiava sistematicamente quem a ignorava, com ~40%
das rotas do Clarke-Wright acima de 9h; ver selecao_modelo.py):
  - até `tamanho_maximo` entregas por sublote (18);
  - até `volume_maximo` caixas por sublote (100);
  - nenhum par de pedidos do mesmo sublote a mais de
    `distancia_maxima_km` (quando ambos têm coordenada; None desliga);
  - tempo estimado da rota (paradas + deslocamento com perna da base)
    até ROTA_TEMPO_MAXIMO_HORAS -- substitui o antigo teto FIXO de 3
    pedidos nível 3 por rota (nível 3 mistura livremente com nível 1/2,
    cada um custando o seu tempo, mesma regra do modelo Atual desde
    20/08); pedido "gigante" (mais caixas que o limite) fica
    em rota exclusiva; nível 4 fica exclusivo TAMBÉM, exceto quando
    junta com outro nível 4 do MESMO endereço de entrega e MESMO
    embarcador (e mesma data de agendamento, quando ambos têm
    agendamento -- pedido do Hugo, 15/08, ajustado 17/08 e 09/09);
    grupo assim que soma volume de veículo grande sai como 1 rota
    desse tipo, sem teto de 100 caixas -- os 5 modelos usam a MESMA
    pré-separação (roteirizacao_dados.separar_pedidos_exclusivos) pra
    essa regra não divergir entre eles.

Diferenciação Grande SP x Viagem (regra 3 do doc): quem chama pode
passar `eh_viagem_fn` (normalmente alocacao_motoristas.
classificar_rota_viagem, com a api_key já aplicada) +
`distancia_maxima_viagem_km` -- quando o sublote candidato é Viagem,
a trava de distância usa o limite de viagem (None = sem limite) em vez
do limite urbano. Sem `eh_viagem_fn`, vale sempre `distancia_maxima_km`.
"""
import logging
import math
from collections.abc import Callable

from roteirizacao_dados import (
    obter_coordenadas, _distancia_km, extrair_cep,
    extrair_volume_caixas, extrair_nivel_dificuldade,
    calcular_km_estimado, separar_pedidos_exclusivos, caixas_e_enderecos,
    estimar_tempo_rota, ROTA_TEMPO_MAXIMO_HORAS, _orcamento_inviavel_por_distancia,
    janela_viavel, ordenar_com_janelas, coords_do_servico, MAX_ITERACOES_2OPT,
)
from regras.tipo_veiculo import classificar_tipo_veiculo

logger = logging.getLogger(__name__)


def _verificar_travas(sublotes: list[list[dict]], tamanho_maximo: int, volume_maximo: int) -> None:
    """Assertions de segurança do doc (seção 6.2) -- rodam ao final de
    cada modelo de agrupamento, pra nenhum sublote sair estourando trava.

    Sublote classificado como veículo grande (regras/tipo_veiculo.py --
    pedido do Hugo, 15/08) é a outra exceção legítima, além do pedido
    "gigante" sozinho: pode ter bem mais que `tamanho_maximo` entregas
    (várias pro MESMO endereço) e `volume_maximo` caixas de propósito --
    as travas que valem pra ele são as do PRÓPRIO tipo (ver
    _extrair_grupos_veiculo_grande em roteirizacao_dados.py), não as de
    última milha."""
    for sublote in sublotes:
        if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
            continue
        assert len(sublote) <= tamanho_maximo, f"Sublote excede {tamanho_maximo} entregas"
        caixas = sum(extrair_volume_caixas(s) for s in sublote)
        # Pedido gigante sozinho (rota exclusiva) é a única exceção legítima.
        assert caixas <= volume_maximo or len(sublote) == 1, f"Sublote excede {volume_maximo} caixas"


def _limite_distancia(sublote_candidato: list[dict], distancia_maxima_km: float | None,
                      distancia_maxima_viagem_km: float | None, eh_viagem_fn) -> float | None:
    if eh_viagem_fn is not None and eh_viagem_fn(sublote_candidato):
        return distancia_maxima_viagem_km
    return distancia_maxima_km


def _cabe_na_distancia(servico: dict, sublote_atual: list[dict], distancia_maxima_km: float | None,
                       api_key: str | None, distancia_maxima_viagem_km: float | None = None,
                       eh_viagem_fn=None) -> bool:
    """Mesma regra do _cabe_na_distancia de dividir_em_sublotes (sem
    coordenada não dá pra checar, não bloqueia), com o limite resolvido
    pelo tipo do sublote candidato (Grande SP x Viagem)."""
    limite = _limite_distancia(sublote_atual + [servico], distancia_maxima_km,
                               distancia_maxima_viagem_km, eh_viagem_fn)
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


def _empacotar_ganancioso(ordenados: list[dict], tamanho_maximo: int, volume_maximo: int,
                          distancia_maxima_km: float | None, api_key: str | None,
                          distancia_maxima_viagem_km: float | None = None,
                          eh_viagem_fn=None) -> list[list[dict]]:
    """
    Empacotamento ganancioso IDÊNTICO ao de dividir_em_sublotes
    (roteirizacao_dados.py) -- inclusive orçamento de horas (25/08) e
    distância entre pares. A ÚNICA diferença dos modelos que usam isto é a
    ORDENAÇÃO de entrada (grade 1D -> theta polar etc.). Pedido
    "gigante" e nível 4 já vêm PRÉ-SEPARADOS por quem chama (ver
    roteirizacao_dados.separar_pedidos_exclusivos) -- `ordenados` aqui
    só contém nível 1/2/3.
    """
    sublotes: list[list[dict]] = []
    sublote_atual: list[dict] = []
    caixas_atual = 0

    for servico in ordenados:
        cx_pedido = extrair_volume_caixas(servico)

        cabe_entregas = len(sublote_atual) + 1 <= tamanho_maximo
        cabe_caixas = caixas_atual + cx_pedido <= volume_maximo
        cabe_distancia = _cabe_na_distancia(servico, sublote_atual, distancia_maxima_km, api_key,
                                            distancia_maxima_viagem_km, eh_viagem_fn)
        # Orçamento de horas (paridade com dividir_em_sublotes, 25/08):
        # recalcula a rota inteira com o candidato, igual à produção.
        # Exceção: destino já inviável só por distância não usa o
        # orçamento pra fragmentar mais (ver _orcamento_inviavel_por_distancia).
        candidato = sublote_atual + [servico]
        cabe_tempo = (estimar_tempo_rota(candidato, api_key) <= ROTA_TEMPO_MAXIMO_HORAS
                     or _orcamento_inviavel_por_distancia(candidato, api_key))
        # Janela de horário (Hugo, 09/09) -- paridade com dividir_em_sublotes.
        cabe_janela = janela_viavel(candidato, api_key)

        if sublote_atual and not (cabe_entregas and cabe_caixas and cabe_distancia and cabe_tempo
                                  and cabe_janela):
            sublotes.append(sublote_atual)
            sublote_atual = []
            caixas_atual = 0

        sublote_atual.append(servico)
        caixas_atual += cx_pedido

    if sublote_atual:
        sublotes.append(sublote_atual)

    return sublotes


def agrupar_por_sweep(servicos: list[dict], base_lat: float, base_lng: float,
                      tamanho_maximo: int = 18, volume_maximo: int = 100,
                      distancia_maxima_km: float | None = 20, api_key: str | None = None,
                      distancia_maxima_viagem_km: float | None = None,
                      eh_viagem_fn=None) -> list[list[dict]]:
    """
    Modelo 1: agrupamento por varredura angular (Sweep) em relação à
    base. Converte cada pedido em ângulo polar theta = atan2(dlat, dlng)
    em relação à base, ordena por theta e preenche rotas sequencialmente
    com o MESMO empacotamento ganancioso (e as mesmas 4 travas) do
    dividir_em_sublotes atual -- a Sweep muda só a ORDENAÇÃO de entrada.

    Serviços sem coordenada: theta = +inf, vão pro final da varredura e
    são agrupados entre si (desempate por CEP, pra manter vizinhança
    postal) -- nunca perde um pedido.

    Pedido "gigante" e nível 4 são pré-separados por
    separar_pedidos_exclusivos ANTES da varredura -- só nível 1/2/3
    participa do sweep (pedido do Hugo, 15/08: junção de nível 4 por
    endereço vale igual pros 5 esquemas).
    """
    sublotes_prontos, demais = separar_pedidos_exclusivos(
        servicos, volume_maximo, distancia_maxima_km, api_key,
        distancia_maxima_viagem_km=distancia_maxima_viagem_km, eh_viagem_fn=eh_viagem_fn,
    )

    def _theta(servico: dict):
        coords = obter_coordenadas(servico, api_key)
        if not coords:
            cep = extrair_cep(servico)
            return (1, float("inf"), int(cep) if cep else float("inf"))
        return (0, math.atan2(coords[0] - base_lat, coords[1] - base_lng), 0.0)

    ordenados = sorted(demais, key=_theta)
    sublotes = sublotes_prontos + _empacotar_ganancioso(ordenados, tamanho_maximo, volume_maximo,
                                     distancia_maxima_km, api_key,
                                     distancia_maxima_viagem_km, eh_viagem_fn)
    _verificar_travas(sublotes, tamanho_maximo, volume_maximo)
    return sublotes


def agrupar_por_savings(servicos: list[dict], base_lat: float, base_lng: float,
                        tamanho_maximo: int = 18, volume_maximo: int = 100,
                        distancia_maxima_km: float | None = 20, api_key: str | None = None,
                        distancia_maxima_viagem_km: float | None = None,
                        eh_viagem_fn=None) -> list[list[dict]]:
    """
    Modelo 2: Clarke-Wright Savings (CVRP). Parte de rotas individuais
    (1 pedido cada) e funde os pares com maior economia
    S_ij = d_0i + d_0j - d_ij (d_0i = distância base -> pedido i),
    processados em ordem decrescente. Cada fusão é validada contra as
    MESMAS 4 travas ANTES de acontecer -- fusão que estoura qualquer
    trava é rejeitada e o algoritmo passa pro próximo par.

    Pedidos sem coordenada: d_0i = 0 (savings zero, ficam por último
    nas fusões) -- nunca perde um pedido.

    Pedido "gigante" e nível 4 são pré-separados por
    separar_pedidos_exclusivos ANTES da fusão -- só nível 1/2/3 entra
    no algoritmo de savings (pedido do Hugo, 15/08: junção de nível 4
    por endereço vale igual pros 5 esquemas).
    """
    sublotes_prontos, demais = separar_pedidos_exclusivos(
        servicos, volume_maximo, distancia_maxima_km, api_key,
        distancia_maxima_viagem_km=distancia_maxima_viagem_km, eh_viagem_fn=eh_viagem_fn,
    )

    n = len(demais)
    if n == 0:
        _verificar_travas(sublotes_prontos, tamanho_maximo, volume_maximo)
        return sublotes_prontos

    coords = [obter_coordenadas(s, api_key) for s in demais]
    d0 = [
        _distancia_km(base_lat, base_lng, *c) if c else 0.0
        for c in coords
    ]

    savings = []
    for i in range(n):
        for j in range(i + 1, n):
            if coords[i] and coords[j]:
                s_ij = d0[i] + d0[j] - _distancia_km(*coords[i], *coords[j])
            else:
                s_ij = 0.0
            savings.append((s_ij, i, j))
    savings.sort(key=lambda t: t[0], reverse=True)

    # Rotas iniciais: 1 pedido cada.
    rota_de = list(range(n))  # índice do pedido -> id da rota
    rotas: dict[int, list[int]] = {i: [i] for i in range(n)}
    caixas: dict[int, int] = {i: extrair_volume_caixas(demais[i]) for i in range(n)}

    def _fusao_valida(indices: list[int]) -> bool:
        sublote = [demais[k] for k in indices]
        if sum(caixas[k] for k in indices) > volume_maximo:
            return False
        if len(sublote) > tamanho_maximo:
            return False
        limite_dist = _limite_distancia(sublote, distancia_maxima_km,
                                        distancia_maxima_viagem_km, eh_viagem_fn)
        if limite_dist is not None:
            pontos = [coords[k] for k in indices if coords[k]]
            for a in range(len(pontos)):
                for b in range(a + 1, len(pontos)):
                    if _distancia_km(*pontos[a], *pontos[b]) > limite_dist:
                        return False
        # Orçamento de horas por último (é a checagem mais cara): a rota
        # fundida, na ordem de concatenação, precisa caber no dia --
        # exceto quando já é inviável só por distância (destino muito
        # longe da base): nesse caso não bloqueia a fusão, ver
        # _orcamento_inviavel_por_distancia.
        if not (estimar_tempo_rota(sublote, api_key) <= ROTA_TEMPO_MAXIMO_HORAS
                or _orcamento_inviavel_por_distancia(sublote, api_key)):
            return False
        # Janela de horário (Hugo, 09/09) -- só custa quando há janela no sublote.
        return janela_viavel(sublote, api_key)

    for s_ij, i, j in savings:
        ra, rb = rota_de[i], rota_de[j]
        if ra == rb:
            continue
        combinada = rotas[ra] + rotas[rb]
        if not _fusao_valida(combinada):
            continue
        for k in rotas[rb]:
            rota_de[k] = ra
        rotas[ra] = combinada
        del rotas[rb]

    sublotes = sublotes_prontos + [[demais[k] for k in indices] for indices in rotas.values()]
    _verificar_travas(sublotes, tamanho_maximo, volume_maximo)
    return sublotes


def _distancia_da_base(servico: dict, base_lat: float, base_lng: float,
                       api_key: str | None) -> float:
    """Mesma regra do ordenar_por_distancia_base de produção: coordenada
    embutida no serviço quando houver, obter_coordenadas como reserva,
    -1 (vai pro final da ordem decrescente) sem coordenada nenhuma."""
    lat, lng = servico.get("latitude"), servico.get("longitude")
    if lat and lng:
        try:
            return _distancia_km(float(lat), float(lng), base_lat, base_lng)
        except (TypeError, ValueError):
            pass
    coords = obter_coordenadas(servico, api_key)
    if coords:
        return _distancia_km(coords[0], coords[1], base_lat, base_lng)
    return -1.0


_coords_do_servico = coords_do_servico  # mantido pelo nome antigo (benchmark/laboratório)


def ordenar_2opt(servicos: list[dict], base_lat: float, base_lng: float,
                 api_key: str | None = None) -> list[dict]:
    """
    Modelo 3: sequenciamento 2-opt. Parte da ordem farthest-first atual
    (mais longe da base primeiro) e elimina cruzamentos revertendo
    segmentos [i, j] sempre que isso reduzir a distância total do
    trajeto base -> p1 -> ... -> pN -> base.

    A posição 0 (entrega mais distante) NUNCA é movida -- requisito de
    negócio: o motorista sai da base direto pro ponto mais longe e vai
    "esvaziando" no caminho de volta. Serviço sem coordenada permanece
    na posição do farthest-first (segmento que o contenha não é
    candidato a reversão).

    Desde 09/09 (janela de horário do cliente, pedido do Hugo) o miolo
    vive em roteirizacao_dados.ordenar_com_janelas -- que é ESTE 2-opt
    quando nenhum pedido tem janela, e um 2-opt + realocação com
    objetivo km + atraso + espera quando tem (o BUG de 14/08 do
    serviço sem coordenada em j+1 continua coberto lá). Esta função
    fica como ponto de entrada de sempre pra selecao_modelo/
    criar_rotas_diarias/rascunhos_rota/benchmark.
    """
    return ordenar_com_janelas(servicos, base_lat, base_lng, api_key)


def agrupar_por_cep(servicos: list[dict], base_lat: float, base_lng: float,
                    tamanho_maximo: int = 18, volume_maximo: int = 100,
                    distancia_maxima_km: float | None = 20, api_key: str | None = None,
                    distancia_maxima_viagem_km: float | None = None,
                    eh_viagem_fn=None, digitos_cep: int = 5) -> list[list[dict]]:
    """
    Modelo 4: agrupamento por CEP real -- ordena os pedidos pelo CEP
    (prefixo de `digitos_cep` dígitos primeiro, 5 por padrão -- nível de
    bairro dos Correios; CEP completo como desempate dentro do mesmo
    prefixo) e preenche rotas sequencialmente com o MESMO empacotamento
    ganancioso (e as mesmas 4 travas) dos outros modelos deste módulo --
    a diferença é só a ORDENAÇÃO de entrada. `base_lat`/`base_lng` não
    entram na ordenação (fazem parte da assinatura só pra manter a MESMA
    interface dos outros modelos, que quem chama usa de forma uniforme).

    Serviços sem CEP reconhecível (extrair_cep retorna None): vão pro
    final da ordenação -- nunca perde um pedido.

    Pedido "gigante" e nível 4 são pré-separados por
    separar_pedidos_exclusivos ANTES da ordenação por CEP -- só nível
    1/2/3 participa (pedido do Hugo, 15/08: junção de nível 4 por
    endereço vale igual pros 5 esquemas).
    """
    sublotes_prontos, demais = separar_pedidos_exclusivos(
        servicos, volume_maximo, distancia_maxima_km, api_key,
        distancia_maxima_viagem_km=distancia_maxima_viagem_km, eh_viagem_fn=eh_viagem_fn,
    )

    def _chave_cep(servico: dict):
        cep = extrair_cep(servico)
        if not cep:
            return (1, float("inf"), float("inf"))
        return (0, int(cep[:digitos_cep]), int(cep))

    ordenados = sorted(demais, key=_chave_cep)
    sublotes = sublotes_prontos + _empacotar_ganancioso(ordenados, tamanho_maximo, volume_maximo,
                                     distancia_maxima_km, api_key,
                                     distancia_maxima_viagem_km, eh_viagem_fn)
    _verificar_travas(sublotes, tamanho_maximo, volume_maximo)
    return sublotes


def agrupar_por_kmeans(servicos: list[dict], base_lat: float, base_lng: float,
                       tamanho_maximo: int = 18, volume_maximo: int = 100,
                       distancia_maxima_km: float | None = 20, api_key: str | None = None,
                       distancia_maxima_viagem_km: float | None = None,
                       eh_viagem_fn=None, tamanho_alvo_cluster: int = 14,
                       max_iteracoes: int = 50) -> list[list[dict]]:
    """
    Modelo 5: clustering geográfico K-means (Lloyd's, distância
    haversine, puro Python -- sem numpy/scikit-learn, dá conta tranquilo
    do volume diário, sempre <200 pedidos). Agrupa por proximidade REAL
    minimizando a dispersão dentro de cada cluster -- diferente da grade
    fixa de 0.1° do modelo Atual (agrupar_por_regiao), que corta/junta
    pontos arbitrariamente na borda da célula.

    K (número de clusters) é derivado do total de pedidos geolocalizados
    dividido por `tamanho_alvo_cluster` (14 por padrão -- abaixo do
    tamanho_maximo de 18, dá folga pro empacotamento final ajustar sem
    estourar a trava). Sementes iniciais determinísticas (farthest-point
    a partir da base, sem RNG) -- a mesma execução sempre produz o mesmo
    resultado, importante pra tela do laboratório não mudar a cada reload.

    Depois de convergir (ou atingir `max_iteracoes`), ordena os pedidos
    por (cluster, distância ao centroide) e alimenta o MESMO
    empacotamento ganancioso (e as mesmas 4 travas) dos outros modelos --
    cluster maior que tamanho_maximo/volume_maximo é subdividido pelo
    empacotador, sem caminho de código novo.

    Serviços sem coordenada: ficam de fora do clustering e vão pro final
    da ordenação (por CEP, mesmo padrão dos outros modelos) -- nunca
    perde um pedido.

    Pedido "gigante" e nível 4 são pré-separados por
    separar_pedidos_exclusivos ANTES do clustering -- só nível 1/2/3
    participa (pedido do Hugo, 15/08: junção de nível 4 por endereço
    vale igual pros 5 esquemas).
    """
    sublotes_prontos, demais = separar_pedidos_exclusivos(
        servicos, volume_maximo, distancia_maxima_km, api_key,
        distancia_maxima_viagem_km=distancia_maxima_viagem_km, eh_viagem_fn=eh_viagem_fn,
    )

    com_coords: list[dict] = []
    pontos: list[tuple[float, float]] = []
    sem_coords: list[dict] = []
    for s in demais:
        c = obter_coordenadas(s, api_key)
        if c:
            com_coords.append(s)
            pontos.append(c)
        else:
            sem_coords.append(s)

    def _chave_cep(servico: dict):
        cep = extrair_cep(servico)
        return int(cep) if cep else float("inf")

    if not pontos:
        ordenados = sorted(sem_coords, key=_chave_cep)
    else:
        k = max(1, round(len(pontos) / tamanho_alvo_cluster))

        # Sementes determinísticas (farthest-point a partir da base) --
        # sem RNG, mesma execução sempre produz o mesmo resultado.
        centroides = [max(pontos, key=lambda p: _distancia_km(*p, base_lat, base_lng))]
        while len(centroides) < k:
            candidato = max(pontos, key=lambda p: min(_distancia_km(*p, *c) for c in centroides))
            centroides.append(candidato)

        atribuicao = [-1] * len(pontos)  # -1 força a 1ª rodada a "mudar"
        for _iteracao in range(max_iteracoes):
            nova_atribuicao = [
                min(range(len(centroides)), key=lambda j: _distancia_km(*ponto, *centroides[j]))
                for ponto in pontos
            ]
            if nova_atribuicao == atribuicao:
                break
            atribuicao = nova_atribuicao

            novos_centroides = []
            for j in range(k):
                membros = [pontos[i] for i in range(len(pontos)) if atribuicao[i] == j]
                if membros:
                    novos_centroides.append((
                        sum(p[0] for p in membros) / len(membros),
                        sum(p[1] for p in membros) / len(membros),
                    ))
                else:
                    novos_centroides.append(centroides[j])  # cluster vazio -- mantém a semente
            centroides = novos_centroides

        indices_ordenados = sorted(
            range(len(pontos)),
            key=lambda i: (atribuicao[i], _distancia_km(*pontos[i], *centroides[atribuicao[i]])),
        )
        ordenados = [com_coords[i] for i in indices_ordenados]
        ordenados.extend(sorted(sem_coords, key=_chave_cep))

    sublotes = sublotes_prontos + _empacotar_ganancioso(ordenados, tamanho_maximo, volume_maximo,
                                     distancia_maxima_km, api_key,
                                     distancia_maxima_viagem_km, eh_viagem_fn)
    _verificar_travas(sublotes, tamanho_maximo, volume_maximo)
    return sublotes


def avaliar_candidatos(servicos: list[dict], candidatos: dict[str, Callable[[], list[list[dict]]]],
                       base_lat: float, base_lng: float, api_key: str | None,
                       tamanho_maximo: int, volume_maximo: int, label: str = "") -> dict[str, dict]:
    """
    Roda TODOS os candidatos de agrupamento (cada um uma função sem
    argumentos, com os pedidos/parâmetros já capturados no closure de
    quem chama -- mesmo padrão de selecao_modelo.py::escolher_melhor_
    modelo), sequencia cada um com ordenar_2opt, valida as 4 travas +
    cobertura total de pedidos, e devolve as métricas de TODOS os
    candidatos que sobreviverem -- diferente de escolher_melhor_modelo,
    que só devolve 1 vencedor. Generalização do loop que já existia em
    selecao_modelo.py, pensada pro laboratório de roteirização
    (painel_agentes/laboratorio_rotas.py), que precisa mostrar TODOS os
    esquemas lado a lado pro Hugo comparar, não escolher sozinho.

    NÃO é chamada por selecao_modelo.py nem por nenhum fluxo de
    produção -- o dict de candidatos de lá continua próprio e intocado.

    Candidato que estourar exceção ou falhar validação é DESCARTADO
    (logado, nunca propaga) -- mesma filosofia de segurança de
    escolher_melhor_modelo: um esquema ruim não derruba a comparação
    inteira.

    Retorna {nome: {"sublotes", "rotas", "entregas", "caixas", "km_total",
    "km_medio"}} só com os candidatos que sobreviveram.
    """
    ids_originais = {s["id"] for s in servicos}
    avaliacoes: dict[str, dict] = {}
    for nome, fn in candidatos.items():
        try:
            sublotes = fn()
            sublotes = [ordenar_2opt(s, base_lat, base_lng, api_key) for s in sublotes]

            for sublote in sublotes:
                if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
                    continue  # veículo grande (regras/tipo_veiculo.py) -- travas de última milha não valem
                assert len(sublote) <= tamanho_maximo, f"sublote com {len(sublote)} entregas (máx {tamanho_maximo})"
                caixas_sublote = sum(extrair_volume_caixas(s) for s in sublote)
                assert caixas_sublote <= volume_maximo or len(sublote) == 1, \
                    f"sublote com {caixas_sublote} caixas (máx {volume_maximo})"
            ids_alocados = [s["id"] for sub in sublotes for s in sub]
            assert len(ids_alocados) == len(set(ids_alocados)), "pedido duplicado entre sublotes"
            assert ids_originais == set(ids_alocados), "pedido perdido no agrupamento"

            total_rotas = len(sublotes)
            total_km = sum(calcular_km_estimado(s, base_lat, base_lng, api_key) for s in sublotes)
            avaliacoes[nome] = {
                "sublotes": sublotes,
                "rotas": total_rotas,
                "entregas": len(ids_alocados),
                "caixas": sum(extrair_volume_caixas(s) for sub in sublotes for s in sub),
                "km_total": total_km,
                "km_medio": total_km / total_rotas if total_rotas else 0.0,
            }
        except Exception as e:
            logger.error(f"[{label}] Modelo '{nome}' descartado da comparação: {e}")
    return avaliacoes
