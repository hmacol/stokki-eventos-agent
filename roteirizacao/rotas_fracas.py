# -*- coding: utf-8 -*-
"""
rotas_fracas.py

Rota fraca (Hugo, 29/09 -- spec docs/superpowers/specs/
2026-09-29-rotas-fracas-design.md): rota com poucos pedidos E poucas
caixas ao mesmo tempo. Pouco pedido sozinho nao basta -- medido em
producao (30/08 a 29/09): 30 das 58 rotas com ate 5 pedidos levavam mais
de 40 caixas, ou seja, carga cheia com poucas paradas.

Tres saidas, nesta ordem:
  1. juntar: distribuir os pedidos nas rotas vizinhas com folga de
     distancia e de paradas (absorver_rotas_fracas);
  2. segurar: adiar os pedidos por 1 dia util, dentro do prazo de 3 dias
     uteis da entrada (motivo_nao_segurar decide; quem grava e o
     criar_rotas_diarias.main, so no job automatico);
  3. avisar: a rota sai com o motivo gravado no rascunho.

Retorno rapido: ROTAS_FRACAS_ATIVO = False volta ao comportamento de
antes. SEGURAR_ATIVO liga so o adiamento.
"""
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

from roteirizacao_dados import (  # noqa: E402
    extrair_volume_caixas, macro_regiao_do_servico, MACRO_GRANDE_SP,
    obter_coordenadas, _distancia_km, macro_regiao_predominante_do_sublote,
)
from otimizacao_rotas import ordenar_2opt  # noqa: E402
from polimento_rotas import _rota_polivel, _rota_valida, _melhor_insercao, _centroide  # noqa: E402
from regioes_dia_fixo import regra_dia_fixo_do_servico  # noqa: E402

ROTAS_FRACAS_ATIVO = True
SEGURAR_ATIVO = False            # liga depois da primeira semana em producao (decisao do Hugo)
PARADAS_ROTA_FRACA = 7
CAIXAS_ROTA_FRACA = 40
FOLGA_DISTANCIA_KM = 20          # distancia entre dois pedidos da rota que recebe (normal: 15)
FOLGA_KM_ACUMULADO_KM = 75       # km acumulado da rota que recebe (normal: 60)
FOLGA_PARADAS_EXTRA = 2          # paradas alem do teto da rodada (16 + 2 = 18)
PRAZO_ENTREGA_DIAS_UTEIS = 3

TZ_BRASILIA = timezone(timedelta(hours=-3))
_PADRAO_REENTREGA = re.compile(r"-R\d+", re.IGNORECASE)


def _caixas(sublote: list[dict]) -> int:
    return sum(extrair_volume_caixas(s) for s in sublote)


def eh_rota_fraca(sublote: list[dict]) -> bool:
    return bool(sublote) and len(sublote) <= PARADAS_ROTA_FRACA and _caixas(sublote) <= CAIXAS_ROTA_FRACA


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
                          eh_viagem_fn=None) -> tuple[list[list[dict]], dict]:
    """Distribui cada rota fraca nas vizinhas da mesma macro-regiao, com
    folga de distancia/km acumulado/paradas SO na rota que recebe. Tudo
    ou nada por rota fraca: se um pedido nao cabe em lugar nenhum, nada
    muda. Diferente do esvaziar do polimento, nao exige queda de km -- o
    ganho aqui e a rota a menos.

    Caixas, 9h, janela e macro-regiao nao cedem -- inclusive por parada:
    um pedido so entra em rota da MESMA macro-regiao dele (uma parada de
    viagem numa fraca de Grande SP nao vai pra receptora de Grande SP,
    que passaria a ser tratada como viagem). Viagem nao ganha folga
    (distancia de viagem ja e sem teto; o km acumulado de viagem fica
    como esta). Nivel 4, veiculo grande e destino inviavel ficam de fora
    (mesmo criterio de polimento_rotas._rota_polivel).

    Nunca perde nem duplica pedido. Devolve (sublotes, relatorio):
    relatorio = {"juntadas": n, "motivos": {id(sublote): texto},
    "receptoras": {id(sublote)}} com o motivo de cada rota que continuou
    fraca e as rotas devolvidas que receberam pedido de fraca (o painel
    aplica nelas o mesmo teto com folga; uma receptora que terminou fraca
    aparece nos dois)."""
    base = (base_lat, base_lng)
    rotas: list[list[dict]] = [list(s) for s in sublotes]
    participantes = [i for i, r in enumerate(rotas) if _rota_polivel(r, api_key, base)]
    macro = {i: macro_regiao_predominante_do_sublote(rotas[i], api_key) for i in participantes}
    distancia_folga = None if distancia_maxima_km is None else max(distancia_maxima_km, FOLGA_DISTANCIA_KM)
    acumulado_folga = None if km_acumulado_maximo is None else max(km_acumulado_maximo, FOLGA_KM_ACUMULADO_KM)
    tamanho_folga = tamanho_maximo + FOLGA_PARADAS_EXTRA

    def _valida(rota: list[dict]) -> bool:
        return _rota_valida(rota, api_key, tamanho_folga, volume_maximo, distancia_folga,
                            distancia_maxima_viagem_km, eh_viagem_fn, base,
                            acumulado_folga, km_acumulado_maximo_viagem)

    def _distancia_da_rota(ponto, rota: list[dict]) -> float:
        centro = _centroide(rota, api_key)
        return _distancia_km(*ponto, *centro) if ponto and centro else 0.0

    juntadas = 0
    motivos_por_indice: dict[int, str] = {}
    receptoras_idx: set[int] = set()
    # menor primeiro: a rota mais fraca e a que mais precisa de lugar
    fracas = sorted((i for i in participantes if eh_rota_fraca(rotas[i])),
                    key=lambda i: (len(rotas[i]), _caixas(rotas[i]), i))
    for i in fracas:
        if not rotas[i] or not eh_rota_fraca(rotas[i]):
            continue  # ja foi absorvida, ou recebeu outra fraca e deixou de ser
        vizinhas = [j for j in participantes if j != i and rotas[j] and macro[j] == macro[i]]
        # as listas sao TROCADAS a cada insercao (nunca mutadas), entao
        # guardar a referencia basta pra desfazer
        backup = {j: rotas[j] for j in vizinhas}
        coube = bool(vizinhas)
        for parada in rotas[i]:
            if not coube:
                break
            ponto = obter_coordenadas(parada, api_key)
            macro_parada = macro_regiao_do_servico(parada, api_key)
            coube = False
            mesma_macro = [j for j in vizinhas if macro[j] == macro_parada]
            for j in sorted(mesma_macro, key=lambda j: (_distancia_da_rota(ponto, rotas[j]), j)):
                _, candidata = _melhor_insercao(parada, rotas[j], base, api_key)
                sequenciada = ordenar_2opt(candidata, base_lat, base_lng, api_key)
                if _valida(sequenciada):
                    rotas[j] = sequenciada
                    coube = True
                    break
        if coube:
            rotas[i] = []
            juntadas += 1
            receptoras_idx.update(j for j in vizinhas if rotas[j] is not backup[j])
        else:
            for j, original in backup.items():
                rotas[j] = original
            motivos_por_indice[i] = _motivo_nao_juntou(rotas[i], [backup[j] for j in vizinhas], api_key)

    # motivo so vale pra quem TERMINOU fraca (uma fraca que falhou pode
    # ter recebido outra depois e deixado de ser)
    motivos = {id(rotas[i]): m for i, m in motivos_por_indice.items() if rotas[i] and eh_rota_fraca(rotas[i])}
    # receptora que depois foi absorvida por outra some da saida
    receptoras = {id(rotas[j]) for j in receptoras_idx if rotas[j]}
    return [r for r in rotas if r], {"juntadas": juntadas, "motivos": motivos, "receptoras": receptoras}
