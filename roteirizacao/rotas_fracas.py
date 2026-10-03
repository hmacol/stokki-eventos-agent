# -*- coding: utf-8 -*-
"""
rotas_fracas.py

Rota fraca (Hugo, 29/09 -- spec docs/superpowers/specs/
2026-09-29-rotas-fracas-design.md): rota com poucos pedidos E poucas
caixas ao mesmo tempo. Pouco pedido sozinho nao basta -- medido em
producao (30/08 a 29/09): 30 das 58 rotas com ate 5 pedidos levavam mais
de 40 caixas, ou seja, carga cheia com poucas paradas. Desde 03/10
(Hugo) tambem precisa ter menos de 5h estimadas: rota curta em paradas
mas longa em tempo ja ocupa o dia do motorista.

Tres saidas, nesta ordem:
  1. juntar: distribuir os pedidos nas rotas vizinhas com folga de
     distancia, paradas, caixas e tempo (absorver_rotas_fracas);
  2. segurar: adiar os pedidos por 1 dia util, dentro do prazo de 3 dias
     uteis da entrada (motivo_nao_segurar decide; quem grava e o
     criar_rotas_diarias.main, so no job automatico);
  3. avisar: a rota sai com o motivo gravado no rascunho.

Retorno rapido: ROTAS_FRACAS_ATIVO = False volta ao comportamento de
antes. SEGURAR_ATIVO liga so o adiamento.
"""
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

from roteirizacao_dados import (  # noqa: E402
    extrair_volume_caixas, macro_regiao_do_servico, MACRO_GRANDE_SP,
    obter_coordenadas, _distancia_km, macro_regiao_predominante_do_sublote, estimar_tempo_rota,
)
from otimizacao_rotas import ordenar_2opt  # noqa: E402
from polimento_rotas import _rota_polivel, _rota_valida, _melhor_insercao, _centroide  # noqa: E402
from regioes_dia_fixo import regra_dia_fixo_do_servico  # noqa: E402

ROTAS_FRACAS_ATIVO = True
SEGURAR_ATIVO = True             # ligado em 03/10 pelo Hugo
PARADAS_ROTA_FRACA = 7
CAIXAS_ROTA_FRACA = 40
HORAS_ROTA_FRACA = 5.0           # fraca so com MENOS de 5h estimadas (Hugo, 03/10)
FOLGA_DISTANCIA_KM = 20          # distancia entre dois pedidos da rota que recebe (normal: 15)
FOLGA_KM_ACUMULADO_KM = 75       # km acumulado da rota que recebe (normal: 60)
FOLGA_PARADAS_EXTRA = 2          # paradas alem do teto da rodada (16 + 2 = 18)
FOLGA_CAIXAS_EXTRA = 10          # caixas alem do teto (100 + 10 = 110); continua Fiorino (Hugo, 03/10)
FOLGA_TEMPO_MAXIMO_HORAS = 10.5  # tempo estimado da rota que recebe (normal: 9h) (Hugo, 03/10)
PRAZO_ENTREGA_DIAS_UTEIS = 3

TZ_BRASILIA = timezone(timedelta(hours=-3))
_PADRAO_REENTREGA = re.compile(r"-R\d+", re.IGNORECASE)


def _caixas(sublote: list[dict]) -> int:
    return sum(extrair_volume_caixas(s) for s in sublote)


def eh_rota_fraca(sublote: list[dict], api_key: str | None = None,
                  base: tuple[float, float] | None = None) -> bool:
    """Ate 7 pedidos E ate 40 caixas E menos de 5h estimadas
    (roteirizacao_dados.estimar_tempo_rota, que inclui a perna da base --
    vale tambem pra rota de 1 parada). O tempo so e estimado quando os
    dois primeiros cortes ja passaram."""
    if not sublote or len(sublote) > PARADAS_ROTA_FRACA or _caixas(sublote) > CAIXAS_ROTA_FRACA:
        return False
    return estimar_tempo_rota(sublote, api_key, base) < HORAS_ROTA_FRACA


def resumo_da_rota(sublote: list[dict]) -> str:
    n, cx = len(sublote), _caixas(sublote)
    return f"{n} {'pedido' if n == 1 else 'pedidos'}, {cx} {'caixa' if cx == 1 else 'caixas'}"


def data_entrada(servico: dict) -> date | None:
    """Dia (Brasilia) em que o pedido entrou. created_at da Vuupt vem em
    UTC sem fuso (confirmado em producao, 10/09 -- ver
    incrementar_rotas._data_criacao); valor com fuso e respeitado."""
    valor = servico.get("created_at")
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_BRASILIA).date()


def proximo_dia_util(d: date) -> date:
    """Dia util estritamente depois de `d` (sem calendario de feriados,
    igual ao resto do sistema)."""
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prazo_final(entrada: date) -> date:
    """Ultimo dia em que o pedido pode ser ENTREGUE: entrada + 3 dias
    uteis. Entrada no fim de semana conta a partir da segunda."""
    atual = entrada
    while atual.weekday() >= 5:
        atual += timedelta(days=1)
    for _ in range(PRAZO_ENTREGA_DIAS_UTEIS):
        atual = proximo_dia_util(atual)
    return atual


def motivo_nao_segurar(servico: dict, data_alvo: date, ja_segurados: set[str],
                       api_key: str | None = None) -> str | None:
    """None = o pedido pode esperar 1 dia util. Senao, o motivo (texto
    curto, vai pra etiqueta da rota). Na duvida, nao segura."""
    if servico.get("scheduled_start"):
        return "tem agendamento"
    if servico.get("recreated_order_origin_id") or _PADRAO_REENTREGA.search(str(servico.get("code") or "")):
        return "é reentrega"
    if any(c in ja_segurados for c in pedidos_dedicados.codigos_do_servico(servico)):
        return "já foi segurado uma vez"
    if regra_dia_fixo_do_servico(servico):
        return "é de região de dia fixo"
    if macro_regiao_do_servico(servico, api_key) != MACRO_GRANDE_SP:
        return "é viagem"
    entrada = data_entrada(servico)
    if entrada is None:
        return "sem data de entrada"
    prazo = prazo_final(entrada)
    if proximo_dia_util(data_alvo) > prazo:
        return f"prazo vence em {prazo:%d/%m}"
    return None


MOTIVO_SEM_VIZINHA = "sem rota vizinha na mesma região"
MOTIVO_NAO_COUBE = "não coube nas vizinhas (distância, paradas, caixas, tempo ou janela)"


def _motivo_nao_juntou(fraca: list[dict], vizinhas: list[list[dict]], api_key: str | None) -> str:
    if not vizinhas:
        return MOTIVO_SEM_VIZINHA
    centro = _centroide(fraca, api_key)
    distancias = [_distancia_km(*centro, *c) for c in (_centroide(v, api_key) for v in vizinhas) if centro and c]
    if distancias and min(distancias) > FOLGA_DISTANCIA_KM:
        return f"vizinha mais próxima a {min(distancias):.0f} km"
    return MOTIVO_NAO_COUBE


def absorver_rotas_fracas(sublotes: list[list[dict]], base_lat: float, base_lng: float, api_key: str | None, *,
                          tamanho_maximo: int, volume_maximo: int, distancia_maxima_km: float | None,
                          distancia_maxima_viagem_km: float | None = None,
                          km_acumulado_maximo: float | None = None,
                          km_acumulado_maximo_viagem: float | None = None,
                          eh_viagem_fn=None, tempo_maximo_s: float = 10.0) -> tuple[list[list[dict]], dict]:
    """Distribui cada rota fraca nas vizinhas da mesma macro-regiao, com
    folga de distancia/km acumulado/paradas/caixas (110) e tempo (10h30)
    SO na rota que recebe. Tudo ou nada por rota fraca: se um pedido nao
    cabe em lugar nenhum, nada muda. Diferente do esvaziar do polimento,
    nao exige queda de km -- o ganho aqui e a rota a menos.

    Os participantes sao calculados UMA vez, no inicio: uma receptora que
    chega a 101-110 caixas (veiculo grande pela regra sem folga, fora de
    _rota_polivel) continua podendo receber de outra fraca -- ela segue
    Fiorino (classificar_tipo_veiculo_com_folga, Hugo 03/10).

    Janela e macro-regiao nao cedem -- inclusive por parada:
    um pedido so entra em rota da MESMA macro-regiao dele (uma parada de
    viagem numa fraca de Grande SP nao vai pra receptora de Grande SP,
    que passaria a ser tratada como viagem). Viagem nao ganha folga
    (distancia de viagem ja e sem teto; o km acumulado de viagem fica
    como esta). Nivel 4, veiculo grande e destino inviavel ficam de fora
    (mesmo criterio de polimento_rotas._rota_polivel).

    Abrir espaco (Hugo, 03/10): pedido p da fraca que nao cabe direto em
    vizinha nenhuma tenta UMA troca -- por vizinha j (ordem de distancia),
    por pedido q de j (ordem na rota; nunca um pedido que veio da propria
    fraca), por terceira rota k (participante, nao vazia, da macro de q,
    ordem de distancia do centroide de k ate q): q vai pra k na melhor
    insercao, p entra em j sem q, e as duas tem que ficar validas com os
    limites de receptora. Primeira combinacao valida vence. j e k viram
    receptoras.

    Teto de tempo (`tempo_maximo_s`): ao estourar, a tentativa em curso e
    desfeita, nenhuma fraca nova e tentada e o relatorio sai com
    "estourou_tempo": True (as fracas nao tentadas ficam sem motivo).

    Nunca perde nem duplica pedido. Devolve (sublotes, relatorio):
    relatorio = {"juntadas": n, "motivos": {id(sublote): texto},
    "receptoras": {id(sublote)}, "estourou_tempo": bool} com o motivo de
    cada rota que continuou fraca e as rotas devolvidas que receberam
    pedido de fraca ou de troca (o painel aplica nelas o mesmo teto com
    folga; uma receptora que terminou fraca aparece nos dois)."""
    inicio = time.monotonic()
    base = (base_lat, base_lng)
    rotas: list[list[dict]] = [list(s) for s in sublotes]
    participantes = [i for i, r in enumerate(rotas) if _rota_polivel(r, api_key, base)]
    macro = {i: macro_regiao_predominante_do_sublote(rotas[i], api_key) for i in participantes}
    distancia_folga = None if distancia_maxima_km is None else max(distancia_maxima_km, FOLGA_DISTANCIA_KM)
    acumulado_folga = None if km_acumulado_maximo is None else max(km_acumulado_maximo, FOLGA_KM_ACUMULADO_KM)
    tamanho_folga = tamanho_maximo + FOLGA_PARADAS_EXTRA
    volume_folga = volume_maximo + FOLGA_CAIXAS_EXTRA

    def _valida(rota: list[dict]) -> bool:
        return _rota_valida(rota, api_key, tamanho_folga, volume_folga, distancia_folga,
                            distancia_maxima_viagem_km, eh_viagem_fn, base,
                            acumulado_folga, km_acumulado_maximo_viagem,
                            tempo_maximo_horas=FOLGA_TEMPO_MAXIMO_HORAS)

    def _distancia_da_rota(ponto, rota: list[dict]) -> float:
        centro = _centroide(rota, api_key)
        return _distancia_km(*ponto, *centro) if ponto and centro else 0.0

    def _tempo_esgotado() -> bool:
        return time.monotonic() - inicio >= tempo_maximo_s

    def _inserir_valida(parada: dict, rota: list[dict]) -> list[dict] | None:
        """Rota com `parada` na melhor insercao, sequenciada, ou None se
        nao fica valida com os limites de receptora."""
        _, candidata = _melhor_insercao(parada, rota, base, api_key)
        sequenciada = ordenar_2opt(candidata, base_lat, base_lng, api_key)
        return sequenciada if _valida(sequenciada) else None

    def _abrir_espaco(parada: dict, i: int, ordem_j: list[int], da_fraca: set[int]) -> bool:
        """Uma troca (ver docstring): grava em rotas[j] e rotas[k] e
        devolve True na primeira combinacao valida. Sem tempo, False."""
        caixas_p = extrair_volume_caixas(parada)
        for j in ordem_j:
            for q in rotas[j]:
                if _tempo_esgotado():
                    return False
                if id(q) in da_fraca:
                    continue
                resto_j = [s for s in rotas[j] if s is not q]
                # filtro barato antes do 2-opt (o _valida reprovaria igual)
                if len(resto_j) + 1 > tamanho_folga or _caixas(resto_j) + caixas_p > volume_folga:
                    continue
                # p em j-sem-q nao depende de k: confere uma vez so por q
                nova_j = _inserir_valida(parada, resto_j)
                if nova_j is None:
                    continue
                ponto_q = obter_coordenadas(q, api_key)
                macro_q = macro_regiao_do_servico(q, api_key)
                caixas_q = extrair_volume_caixas(q)
                ks = [k for k in participantes
                      if k != i and k != j and rotas[k] and macro[k] == macro_q
                      and len(rotas[k]) + 1 <= tamanho_folga and _caixas(rotas[k]) + caixas_q <= volume_folga]
                for k in sorted(ks, key=lambda k: (_distancia_da_rota(ponto_q, rotas[k]), k)):
                    if _tempo_esgotado():
                        return False
                    nova_k = _inserir_valida(q, rotas[k])
                    if nova_k is not None:
                        rotas[j], rotas[k] = nova_j, nova_k
                        return True
        return False

    juntadas = 0
    estourou_tempo = False
    motivos_por_indice: dict[int, str] = {}
    receptoras_idx: set[int] = set()
    # menor primeiro: a rota mais fraca e a que mais precisa de lugar
    fracas = sorted((i for i in participantes if eh_rota_fraca(rotas[i], api_key, base)),
                    key=lambda i: (len(rotas[i]), _caixas(rotas[i]), i))
    for i in fracas:
        if _tempo_esgotado():
            estourou_tempo = True
            break
        if not rotas[i] or not eh_rota_fraca(rotas[i], api_key, base):
            continue  # ja foi absorvida, ou recebeu outra fraca e deixou de ser
        vizinhas = [j for j in participantes if j != i and rotas[j] and macro[j] == macro[i]]
        # snapshot de TODAS as rotas: as listas sao TROCADAS a cada
        # insercao (nunca mutadas), entao guardar as referencias basta pra
        # desfazer -- inclusive a terceira rota k da troca
        snapshot = list(rotas)
        da_fraca = {id(s) for s in rotas[i]}
        coube = bool(vizinhas)
        for parada in snapshot[i]:
            if not coube:
                break
            ponto = obter_coordenadas(parada, api_key)
            macro_parada = macro_regiao_do_servico(parada, api_key)
            coube = False
            mesma_macro = [j for j in vizinhas if macro[j] == macro_parada]
            ordem_j = sorted(mesma_macro, key=lambda j: (_distancia_da_rota(ponto, rotas[j]), j))
            for j in ordem_j:
                if _tempo_esgotado():
                    break
                sequenciada = _inserir_valida(parada, rotas[j])
                if sequenciada is not None:
                    rotas[j] = sequenciada
                    coube = True
                    break
            if not coube:
                coube = _abrir_espaco(parada, i, ordem_j, da_fraca)
            if not coube and _tempo_esgotado():
                estourou_tempo = True
        if coube:
            rotas[i] = []
            juntadas += 1
            receptoras_idx.update(k for k in participantes if k != i and rotas[k] is not snapshot[k])
        else:
            rotas[:] = snapshot
            if estourou_tempo:
                break  # tentativa interrompida pelo teto de tempo: sem motivo, nada mais e tentado
            motivos_por_indice[i] = _motivo_nao_juntou(rotas[i], [rotas[j] for j in vizinhas], api_key)

    # motivo so vale pra quem TERMINOU fraca (uma fraca que falhou pode
    # ter recebido outra depois e deixado de ser)
    motivos = {id(rotas[i]): m for i, m in motivos_por_indice.items() if rotas[i] and eh_rota_fraca(rotas[i], api_key, base)}
    # receptora que depois foi absorvida por outra some da saida
    receptoras = {id(rotas[j]) for j in receptoras_idx if rotas[j]}
    return [r for r in rotas if r], {"juntadas": juntadas, "motivos": motivos, "receptoras": receptoras,
                                     "estourou_tempo": estourou_tempo}
