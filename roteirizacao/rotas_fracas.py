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
import logging
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
import pedidos_dedicados  # noqa: E402  (normalizacao do codigo do pedido)

from roteirizacao_dados import extrair_volume_caixas, macro_regiao_do_servico, MACRO_GRANDE_SP  # noqa: E402
from regioes_dia_fixo import regra_dia_fixo_do_servico  # noqa: E402

logger = logging.getLogger(__name__)

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
