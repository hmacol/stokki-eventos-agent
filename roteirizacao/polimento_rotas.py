# -*- coding: utf-8 -*-
"""
polimento_rotas.py

Busca local ENTRE rotas (Hugo, 18/09 -- spec docs/superpowers/specs/
2026-09-18-recalibracao-roteirizacao-design.md, secao 3.4). Roda depois
do modelo vencedor e da fusao de sublotes pequenos, sobre as rotas de
UMA particao do dia. Nenhum dos 5 modelos olha uma rota em relacao as
outras -- e por isso 20-30% das paradas tinham a vizinha mais proxima em
OUTRA rota (medido em producao, 11-17/09).

Movimentos, aceitos so quando o km total das duas rotas envolvidas cai
pelo menos `ganho_minimo_km` E as duas continuam validas (_rota_valida):
  1. realocar: tirar uma parada da rota A e inserir na rota B na posicao
     de menor acrescimo;
  2. trocar: permutar uma parada de A com uma de B;
  3. esvaziar: rota com 1 ou 2 paradas tenta realocar TODAS nas
     vizinhas -- movimento composto, aceito se todas couberem e o km
     total das rotas envolvidas cair (tirar a rota inteira elimina a
     perna da base, ganho que a realocacao parada a parada nao enxerga).
So rotas "poliveis" participam (_rota_polivel: sem nivel 4, sem veiculo
grande, sem destino inviavel por distancia); so rotas da MESMA
macro-regiao trocam paradas. Deterministico (laco em ordem de indice,
sem aleatoriedade). Teto de tempo (`tempo_maximo_s`) pra nao estourar a
janela das 22h.

Custo: o filtro rapido (km sem resequenciar) roda pra todo candidato; o
resequenciamento (ordenar_2opt) + validacao completa so roda quando o
filtro aponta ganho.
"""
import logging
import time

from roteirizacao_dados import (
    obter_coordenadas, _distancia_km, calcular_km_estimado, extrair_volume_caixas,
    extrair_nivel_dificuldade, NIVEL_ROTA_EXCLUSIVA, estimar_tempo_rota, ROTA_TEMPO_MAXIMO_HORAS,
    _orcamento_inviavel_por_distancia, janela_respeitada, caixas_e_enderecos,
    macro_regiao_predominante_do_sublote, _km_acumulado_sequencial,
)
from otimizacao_rotas import limite_distancia, ordenar_2opt
from regras.tipo_veiculo import classificar_tipo_veiculo

logger = logging.getLogger(__name__)

PARADAS_ROTA_ESVAZIAVEL = 2  # rota com ate este numero de paradas tenta se esvaziar nas vizinhas
TOLERANCIA_PIORA_KM = 1e-6  # rede de seguranca final -- ver comentario no final de polir_entre_rotas


def _rota_polivel(sublote: list[dict], api_key: str | None,
                  base: tuple[float, float] | None = None) -> bool:
    """Mesmos 3 criterios de roteirizacao_dados.exige_orcamento_horas,
    SEM a exclusao de rota de 1 parada (rota de 1 parada pode ser origem
    -- esvaziar -- e destino). `base` passada explicitamente (18/09, fix
    final) em vez de depender da global COORDS_BASE do modulo de dados
    -- ver comentario de _rota_valida."""
    if not sublote:
        return False
    if any(extrair_nivel_dificuldade(s) == NIVEL_ROTA_EXCLUSIVA for s in sublote):
        return False
    if classificar_tipo_veiculo(*caixas_e_enderecos(sublote)) is not None:
        return False
    if _orcamento_inviavel_por_distancia(sublote, api_key, base):
        return False
    return True


def _rota_valida(sublote: list[dict], api_key: str | None, tamanho_maximo: int, volume_maximo: int,
                 distancia_maxima_km: float | None, distancia_maxima_viagem_km: float | None,
                 eh_viagem_fn, base: tuple[float, float] | None = None,
                 km_acumulado_maximo: float | None = None,
                 km_acumulado_maximo_viagem: float | None = None) -> bool:
    """Travas de producao na ORDEM DADA: tamanho, caixas, distancia
    par-a-par, km acumulado, orcamento de horas, janela. Rota vazia e
    valida (vai sumir).

    `base` passada explicitamente pras chamadas de orcamento de horas e
    janela (fix final, 20/09): antes elas nao recebiam `base` e liam a
    global COORDS_BASE do modulo de dados -- hoje os dois valores
    sempre coincidem (definir_coords_base roda antes), mas nada
    obrigava isso, e o painel (processo longo, chamadas concorrentes)
    e onde essa suposicao quebraria primeiro."""
    if not sublote:
        return True
    if len(sublote) > tamanho_maximo:
        return False
    caixas = sum(extrair_volume_caixas(s) for s in sublote)
    # Pedido gigante sozinho (rota exclusiva) e a unica excecao legitima
    # -- mesmo padrao de selecao_modelo._validar e
    # otimizacao_rotas.verificar_particao, pra nao divergir (fix final,
    # 20/09: antes so essas duas cópias tinham a excecao).
    if caixas > volume_maximo and len(sublote) != 1:
        return False
    limite = limite_distancia(sublote, distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn)
    if limite is not None:
        pontos = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
        for a in range(len(pontos)):
            for b in range(a + 1, len(pontos)):
                if _distancia_km(*pontos[a], *pontos[b]) > limite:
                    return False
    # Km ACUMULADO sequencial (fix final, 20/09): mesma salvaguarda
    # contra zigzag que dividir_em_sublotes/fundir_sublotes_pequenos ja
    # aplicam (KM_ACUMULADO_MAXIMO_ROTA_KM/_VIAGEM_KM em
    # criar_rotas_diarias.py) -- sem isso o polimento, por ser a ULTIMA
    # etapa, podia deixar uma rota crescer o acumulado sem limite (so a
    # distancia PAR-A-PAR acima era conferida). Mesma decisao urbano x
    # viagem do limite par-a-par, via limite_distancia.
    limite_acumulado = limite_distancia(sublote, km_acumulado_maximo, km_acumulado_maximo_viagem, eh_viagem_fn)
    if limite_acumulado is not None and _km_acumulado_sequencial(sublote, api_key) > limite_acumulado:
        return False
    if len(sublote) > 1 and not (estimar_tempo_rota(sublote, api_key, base) <= ROTA_TEMPO_MAXIMO_HORAS
                                 or _orcamento_inviavel_por_distancia(sublote, api_key, base)):
        return False
    return janela_respeitada(sublote, api_key, base)


def _km(sublote: list[dict], base: tuple[float, float], api_key: str | None) -> float:
    return calcular_km_estimado(sublote, base[0], base[1], api_key) if sublote else 0.0


def _centroide(sublote: list[dict], api_key: str | None) -> tuple[float, float] | None:
    pontos = [c for c in (obter_coordenadas(s, api_key) for s in sublote) if c]
    if not pontos:
        return None
    return (sum(p[0] for p in pontos) / len(pontos), sum(p[1] for p in pontos) / len(pontos))


def _melhor_insercao(parada: dict, sublote: list[dict], base: tuple[float, float],
                     api_key: str | None) -> tuple[float, list[dict]]:
    """(km, sublote_novo) inserindo `parada` na posicao de menor km."""
    melhor: tuple[float, list[dict]] | None = None
    for pos in range(len(sublote) + 1):
        candidato = sublote[:pos] + [parada] + sublote[pos:]
        km = _km(candidato, base, api_key)
        if melhor is None or km < melhor[0]:
            melhor = (km, candidato)
    return melhor  # type: ignore[return-value]


def polir_entre_rotas(sublotes: list[list[dict]], base_lat: float, base_lng: float, api_key: str | None, *,
                      tamanho_maximo: int, volume_maximo: int, distancia_maxima_km: float | None,
                      distancia_maxima_viagem_km: float | None = None,
                      km_acumulado_maximo: float | None = None,
                      km_acumulado_maximo_viagem: float | None = None,
                      eh_viagem_fn=None,
                      tempo_maximo_s: float = 3.0, ganho_minimo_km: float = 0.05
                      ) -> tuple[list[list[dict]], dict]:
    """Ver docstring do modulo. Devolve (sublotes_polidos, resumo). Nunca
    perde nem duplica servico; rotas nao poliveis saem identicas (mesmo
    objeto de lista)."""
    inicio = time.monotonic()
    base = (base_lat, base_lng)
    originais = list(sublotes)
    rotas: list[list[dict]] = [list(s) for s in originais]
    poliveis = [i for i, r in enumerate(rotas) if _rota_polivel(r, api_key, base)]
    # macro-regiao calculada UMA vez, sobre a rota original (a rota muda
    # de conteudo durante o polimento, mas nunca troca de macro-regiao)
    macro = {i: macro_regiao_predominante_do_sublote(originais[i], api_key) for i in poliveis}
    km_antes = sum(_km(r, base, api_key) for r in rotas)
    resumo = {"realocacoes": 0, "trocas": 0, "esvaziadas": 0, "km_antes": km_antes,
              "km_depois": km_antes, "tempo_s": 0.0, "estourou_tempo": False}

    def _valida(rota: list[dict]) -> bool:
        return _rota_valida(rota, api_key, tamanho_maximo, volume_maximo,
                            distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn,
                            base, km_acumulado_maximo, km_acumulado_maximo_viagem)

    def _tempo_esgotado() -> bool:
        if time.monotonic() - inicio >= tempo_maximo_s:
            resumo["estourou_tempo"] = True
            return True
        return False

    def _vizinhas(i: int) -> list[int]:
        ci = _centroide(rotas[i], api_key)
        # Teto de vizinhanca: o mesmo que se aplica a ROTA i pra
        # distancia par-a-par (urbano ou viagem, via limite_distancia) --
        # fix final, 20/09: antes usava sempre distancia_maxima_km fixo,
        # entao a vizinhanca de uma rota de Viagem (cujo teto costuma ser
        # None, sem limite) continuava presa ao raio urbano.
        limite = limite_distancia(rotas[i], distancia_maxima_km, distancia_maxima_viagem_km, eh_viagem_fn)
        saida = []
        for j in poliveis:
            if j == i or not rotas[j] or macro[j] != macro[i]:
                continue
            cj = _centroide(rotas[j], api_key)
            if ci and cj and limite is not None and _distancia_km(*ci, *cj) > 2 * limite:
                continue
            saida.append(j)
        return saida

    def _tentar(i: int, j: int, nova_i: list[dict], nova_j: list[dict], exigir_ganho: bool = True) -> bool:
        """Filtro rapido (km sem resequenciar) -> resequencia -> valida
        -> confere o ganho de novo. True se aplicou."""
        km_atual = _km(rotas[i], base, api_key) + _km(rotas[j], base, api_key)
        if exigir_ganho and _km(nova_i, base, api_key) + _km(nova_j, base, api_key) > km_atual - ganho_minimo_km:
            return False
        seq_i = ordenar_2opt(nova_i, base_lat, base_lng, api_key) if nova_i else []
        seq_j = ordenar_2opt(nova_j, base_lat, base_lng, api_key) if nova_j else []
        if not _valida(seq_i) or not _valida(seq_j):
            return False
        if exigir_ganho and _km(seq_i, base, api_key) + _km(seq_j, base, api_key) > km_atual - ganho_minimo_km:
            return False
        rotas[i], rotas[j] = seq_i, seq_j
        return True

    # 1 e 2: realocar e trocar, ate nao melhorar mais (ou estourar o tempo).
    # Teto de tempo em granularidade fina: alem de por rota i e por vizinha
    # j, tambem dentro do laco de paradas e dentro do laco de troca (esse e
    # quadratico nas paradas e chama _melhor_insercao DUAS vezes por par --
    # de longe o trecho mais caro por par de rotas). Cada _tentar so grava
    # depois de decidir sozinho (atomico), entao parar de checar tempo no
    # meio nao deixa movimento pela metade -- so deixa de tentar mais.
    melhorou = True
    while melhorou and not _tempo_esgotado():
        melhorou = False
        for i in poliveis:
            if not rotas[i] or _tempo_esgotado():
                continue
            for j in _vizinhas(i):
                if _tempo_esgotado():
                    break
                for parada in list(rotas[i]):
                    if _tempo_esgotado():
                        break
                    resto_i = [s for s in rotas[i] if s is not parada]
                    _, cand_j = _melhor_insercao(parada, rotas[j], base, api_key)
                    if _tentar(i, j, resto_i, cand_j):
                        resumo["realocacoes"] += 1
                        melhorou = True
                if not rotas[i] or _tempo_esgotado():
                    break
                trocou = False
                for a in list(rotas[i]):
                    if _tempo_esgotado():
                        break
                    for b in list(rotas[j]):
                        if _tempo_esgotado():
                            break
                        resto_i = [s for s in rotas[i] if s is not a]
                        resto_j = [s for s in rotas[j] if s is not b]
                        _, cand_i = _melhor_insercao(b, resto_i, base, api_key)
                        _, cand_j = _melhor_insercao(a, resto_j, base, api_key)
                        if _tentar(i, j, cand_i, cand_j):
                            resumo["trocas"] += 1
                            melhorou = trocou = True
                            break
                    if trocou or _tempo_esgotado():
                        break

    # 3: esvaziar rotas pequenas nas vizinhas. Movimento COMPOSTO: cada
    # parada sozinha pode nao dar ganho (a rota de origem continua pagando
    # a perna da base), mas tirar TODAS elimina a perna inteira -- por
    # isso o ganho e conferido no conjunto, nao parada a parada.
    for i in poliveis:
        if _tempo_esgotado():
            break
        if not rotas[i] or len(rotas[i]) > PARADAS_ROTA_ESVAZIAVEL:
            continue
        # vizinhanca CONGELADA no inicio da tentativa desta rota: o
        # centroide de rotas[i] se desloca a cada parada que sai dela, e
        # recalcular _vizinhas(i) de novo dentro do laco de colocacao
        # poderia apontar pra uma rota que NAO esta no backup -- _tentar
        # grava incondicionalmente em rotas[j], e um revert que nao cobre
        # essa rota deixa a parada duplicada (bug reproduzido no fix
        # round 1: entrada [1,2,11,21] saiu [1,2,2,11,21]). Perder uma
        # vizinha que so entraria no alcance depois e preco justo:
        # integridade e determinismo valem mais.
        vizinhas_congeladas = _vizinhas(i)
        envolvidas = [i] + vizinhas_congeladas
        backup = {k: list(rotas[k]) for k in envolvidas}
        km_antes_local = sum(_km(rotas[k], base, api_key) for k in envolvidas)
        ok = True
        for parada in list(rotas[i]):
            if _tempo_esgotado():
                ok = False
                break
            colocou = False
            for j in vizinhas_congeladas:
                if _tempo_esgotado():
                    break
                _, cand_j = _melhor_insercao(parada, rotas[j], base, api_key)
                resto_i = [s for s in rotas[i] if s is not parada]
                if _tentar(i, j, resto_i, cand_j, exigir_ganho=False):
                    colocou = True
                    break
            if not colocou:
                ok = False
                break
        km_depois_local = sum(_km(rotas[k], base, api_key) for k in envolvidas)
        # reverte se: alguma parada ficou sem lugar, sobrou algo em rotas[i],
        # ou o km do conjunto nao caiu -- e o revert cobre EXATAMENTE
        # [i] + vizinhas_congeladas, entao nenhuma escrita deste bloco pode
        # ter ido pra fora do que o backup cobre (ver comentario acima)
        if not (ok and not rotas[i] and km_depois_local <= km_antes_local - ganho_minimo_km):
            for k, v in backup.items():
                rotas[k] = v

    resultado = []
    for idx, r in enumerate(rotas):
        if idx not in poliveis:
            resultado.append(originais[idx])  # objeto original, intocado
        elif r:
            resultado.append(r)
    # rota polivel que terminou vazia (por realocacao ou por esvaziamento)
    resumo["esvaziadas"] = sum(1 for idx in poliveis if not rotas[idx])
    resumo["km_depois"] = sum(_km(r, base, api_key) for r in resultado)
    resumo["tempo_s"] = time.monotonic() - inicio
    # rede de seguranca final: todo movimento aceito por _tentar so grava
    # quando o km do par cai (ou, no esvaziamento, quando o km do conjunto
    # cai) -- entao o km de saida NUNCA deveria ficar pior que o de entrada.
    # Isso nao e comportamento esperado, e uma rede contra bug futuro (o
    # mesmo tipo de bug do fix round 1, so que atingindo o km em vez da
    # integridade dos ids): se acontecer mesmo assim, descarta o resultado
    # inteiro e devolve a entrada original intocada, pelos MESMOS objetos.
    resumo["descartado_por_piora"] = False
    if resumo["km_depois"] > resumo["km_antes"] + TOLERANCIA_PIORA_KM:
        resumo["descartado_por_piora"] = True
        resumo["km_depois"] = resumo["km_antes"]
        return list(originais), resumo
    return resultado, resumo
