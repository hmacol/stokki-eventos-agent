# -*- coding: utf-8 -*-
"""
regras/calendario.py

Calendario de dias uteis e feriados, UNICO para o sistema inteiro (decisao
do Hugo, 04/10/2026, na spec do agente analista: "incluir feriados e
considerar para todos como feriado e nao como dia util; nao temos rotas
nos feriados com excecao de necessidade dedicada; esse calendario vale pra
tudo").

Antes disso cada modulo fazia `weekday() >= 5` por conta propria (vigia,
rotas fracas, pipeline, incrementar, relatorio, Torre, insucesso, coleta,
chamados), e feriado contava como dia util em todos eles. Agora todos
chamam este modulo. Regra: NENHUM outro arquivo decide sozinho se um dia
e util -- se precisar de uma variacao, acrescente aqui.

Feriados considerados (operacao na Grande Sao Paulo):
  - nacionais fixos: 01/01, 21/04, 01/05, 07/09, 12/10, 02/11, 15/11,
    20/11 (Consciencia Negra, nacional desde 2024), 25/12;
  - nacionais moveis (a partir da Pascoa): segunda e terca de Carnaval,
    Sexta-feira Santa, Corpus Christi;
  - estado de SP: 09/07;
  - cidade de Sao Paulo: 25/01.
Carnaval e Corpus Christi sao ponto facultativo em lei, mas a operacao
nao roda neles (Hugo, 04/10). Pra tirar ou acrescentar um dia sem mexer
em codigo, use dados/feriados.json:
  {"incluir": {"2026-11-16": "Emenda"}, "excluir": ["2026-02-17"]}

Modulo puro: nada de rede nem banco. O JSON e lido uma vez por processo
(ler `recarregar_extras()` nos testes).
"""
import json
import logging
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dateutil.easter import easter

logger = logging.getLogger(__name__)

_ARQ_EXTRAS = Path(__file__).parent.parent / "dados" / "feriados.json"
_extras: dict | None = None  # {"incluir": {date: nome}, "excluir": {date}}

FERIADOS_FIXOS = {
    (1, 1): "Confraternizacao Universal",
    (1, 25): "Aniversario de Sao Paulo",
    (4, 21): "Tiradentes",
    (5, 1): "Dia do Trabalho",
    (7, 9): "Revolucao Constitucionalista (SP)",
    (9, 7): "Independencia",
    (10, 12): "Nossa Senhora Aparecida",
    (11, 2): "Finados",
    (11, 15): "Proclamacao da Republica",
    (11, 20): "Consciencia Negra",
    (12, 25): "Natal",
}


def _como_date(d) -> date:
    return d.date() if isinstance(d, datetime) else d


def _carregar_extras() -> dict:
    global _extras
    if _extras is not None:
        return _extras
    incluir: dict[date, str] = {}
    excluir: set[date] = set()
    try:
        with open(_ARQ_EXTRAS, encoding="utf-8") as f:
            bruto = json.load(f) or {}
        for k, nome in (bruto.get("incluir") or {}).items():
            incluir[date.fromisoformat(k)] = str(nome or "Feriado extra")
        for k in bruto.get("excluir") or []:
            excluir.add(date.fromisoformat(k))
    except FileNotFoundError:
        pass
    except Exception as e:  # JSON quebrado nao pode derrubar o sistema
        logger.warning(f"Falha ao ler {_ARQ_EXTRAS.name}: {e} -- seguindo sem extras")
    _extras = {"incluir": incluir, "excluir": excluir}
    return _extras


def recarregar_extras() -> None:
    """Esquece o cache de dados/feriados.json (uso em testes e no painel)."""
    global _extras
    _extras = None


def feriados(ano: int) -> dict[date, str]:
    """Todos os feriados do ano, {data: nome}, ja com os extras aplicados."""
    base = {date(ano, m, d): nome for (m, d), nome in FERIADOS_FIXOS.items()}
    pascoa = easter(ano)
    base[pascoa - timedelta(days=48)] = "Carnaval (segunda)"
    base[pascoa - timedelta(days=47)] = "Carnaval (terca)"
    base[pascoa - timedelta(days=2)] = "Sexta-feira Santa"
    base[pascoa + timedelta(days=60)] = "Corpus Christi"
    extras = _carregar_extras()
    for d, nome in extras["incluir"].items():
        if d.year == ano:
            base[d] = nome
    for d in extras["excluir"]:
        base.pop(d, None)
    return dict(sorted(base.items()))


def nome_feriado(d) -> str | None:
    d = _como_date(d)
    return feriados(d.year).get(d)


def eh_feriado(d) -> bool:
    return nome_feriado(d) is not None


def eh_fim_de_semana(d) -> bool:
    return _como_date(d).weekday() >= 5


def eh_dia_util(d) -> bool:
    """Segunda a sexta e nao feriado. Aceita date ou datetime."""
    d = _como_date(d)
    return d.weekday() < 5 and not eh_feriado(d)


def proximo_dia_util(d: date, inclusive: bool = False) -> date:
    """Primeiro dia util depois de `d`. Com inclusive=True, o proprio `d`
    serve se for util (rolar uma data pra frente ate cair em dia util)."""
    atual = d if inclusive else d + timedelta(days=1)
    while not eh_dia_util(atual):
        atual += timedelta(days=1)
    return atual


def dia_util_anterior(d: date, inclusive: bool = False) -> date:
    """Ultimo dia util antes de `d` (ou o proprio, com inclusive=True)."""
    atual = d if inclusive else d - timedelta(days=1)
    while not eh_dia_util(atual):
        atual -= timedelta(days=1)
    return atual


def somar_dias_uteis(inicio, dias: int):
    """`dias` dias uteis depois de `inicio`, no mesmo tipo (date ou
    datetime, mesmo horario). Comecar num dia nao util conta a partir do
    proximo dia util, as 00:00 -- e o comportamento que o vigia ja tinha
    (vigia/regras.py) e que o agente herda."""
    if isinstance(inicio, datetime):
        atual = inicio
        if not eh_dia_util(atual):
            atual = datetime.combine(proximo_dia_util(atual.date()), time(0, 0), tzinfo=inicio.tzinfo)
        for _ in range(dias):
            atual = datetime.combine(proximo_dia_util(atual.date()), atual.time(), tzinfo=inicio.tzinfo)
        return atual
    atual = proximo_dia_util(inicio, inclusive=True)
    for _ in range(dias):
        atual = proximo_dia_util(atual)
    return atual


def contar_dias_uteis(de: date, ate: date) -> int:
    """Dias uteis no intervalo (de, ate], o que falta de `de` ate `ate`.
    `ate` <= `de` da zero."""
    n = 0
    atual = de
    while atual < ate:
        atual += timedelta(days=1)
        if eh_dia_util(atual):
            n += 1
    return n
