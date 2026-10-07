# -*- coding: utf-8 -*-
"""
feriados.py

Calendario de feriados da operacao (Hugo, 07/10/2026: "nao trabalhamos"
em feriado). Unico lugar do projeto que sabe o que e dia util -- antes
cada modulo tinha a sua copia de "weekday() >= 5" e o sistema criava
rota e agendava dia fixo em feriado (12/10/2026 era o primeiro caso).

Feriados considerados:
  - nacionais fixos: 01/01, 21/04, 01/05, 07/09, 12/10, 02/11, 15/11,
    20/11 (Consciencia Negra, nacional desde 2024) e 25/12;
  - moveis pela Pascoa: Carnaval (segunda e terca), Sexta-feira Santa e
    Corpus Christi (ponto facultativo, mas a Fresh Log nao opera);
  - estadual SP: 09/07; municipal Sao Paulo: 25/01.

Quarta-feira de Cinzas opera normalmente. Feriados municipais das cidades
de destino (Santos, Campinas...) nao entram: a base e Sao Paulo.

Datas moveis sao calculadas (algoritmo de Gauss/Meeus), entao o
calendario nao vence com a virada do ano.

Convencao das funcoes (mesma dos helpers antigos que substituiram):
  rolar_para_dia_util(d)   inclusivo -- devolve d se d ja e util
  proximo_dia_util(d)      estrito  -- primeiro dia util DEPOIS de d
  dia_util_anterior(d)     estrito  -- ultimo dia util ANTES de d
  somar_dias_uteis(d, n)   n dias uteis a frente (d em dia nao util conta
                           a partir do util seguinte)
"""
from datetime import date, datetime, timedelta
from functools import lru_cache


def _pascoa(ano: int) -> date:
    """Domingo de Pascoa (calendario gregoriano, algoritmo de Meeus)."""
    a = ano % 19
    b, c = divmod(ano, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return date(ano, mes, dia + 1)


@lru_cache(maxsize=None)
def feriados_do_ano(ano: int) -> dict[date, str]:
    pascoa = _pascoa(ano)
    return {
        date(ano, 1, 1): "Confraternizacao Universal",
        date(ano, 1, 25): "Aniversario de Sao Paulo",
        pascoa - timedelta(days=48): "Carnaval",
        pascoa - timedelta(days=47): "Carnaval",
        pascoa - timedelta(days=2): "Sexta-feira Santa",
        date(ano, 4, 21): "Tiradentes",
        date(ano, 5, 1): "Dia do Trabalho",
        pascoa + timedelta(days=60): "Corpus Christi",
        date(ano, 7, 9): "Revolucao Constitucionalista",
        date(ano, 9, 7): "Independencia do Brasil",
        date(ano, 10, 12): "Nossa Senhora Aparecida",
        date(ano, 11, 2): "Finados",
        date(ano, 11, 15): "Proclamacao da Republica",
        date(ano, 11, 20): "Consciencia Negra",
        date(ano, 12, 25): "Natal",
    }


def _como_date(d) -> date:
    return d.date() if isinstance(d, datetime) else d


def nome_feriado(d) -> str | None:
    d = _como_date(d)
    return feriados_do_ano(d.year).get(d)


def eh_feriado(d) -> bool:
    return nome_feriado(d) is not None


def eh_fim_de_semana(d) -> bool:
    return _como_date(d).weekday() >= 5


def eh_dia_util(d) -> bool:
    """Segunda a sexta e nao feriado."""
    d = _como_date(d)
    return d.weekday() < 5 and not eh_feriado(d)


def rolar_para_dia_util(d: date) -> date:
    """`d` se ja for dia util; senao o primeiro dia util depois dele."""
    while not eh_dia_util(d):
        d += timedelta(days=1)
    return d


def proximo_dia_util(d: date) -> date:
    """Primeiro dia util estritamente DEPOIS de `d`."""
    return rolar_para_dia_util(d + timedelta(days=1))


def dia_util_anterior(d: date) -> date:
    """Ultimo dia util estritamente ANTES de `d`."""
    d -= timedelta(days=1)
    while not eh_dia_util(d):
        d -= timedelta(days=1)
    return d


def somar_dias_uteis(d: date, dias: int) -> date:
    """`dias` dias uteis depois de `d`. Comecar em dia nao util conta a
    partir do dia util seguinte (sabado + 1 util = terca, nao segunda)."""
    atual = rolar_para_dia_util(d)
    for _ in range(dias):
        atual = proximo_dia_util(atual)
    return atual
