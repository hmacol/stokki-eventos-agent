# -*- coding: utf-8 -*-
"""
Regra pura do agendamento automatico (sem rede). Spec:
docs/superpowers/specs/2026-11-02-agendamento-automatico-proximo-dia-util-design.md
Decisao do Hugo (02/11, opcao A): o corte das 14h vale de verdade; a rodada
das 14h05 ainda conta como antes do corte (compara-se a hora de INICIO da
rodada com CORTE_RODADA). `data_saida` (expedition_date da Stokki) e data
minima: se for depois da data do corte, vale ela, rolada pra dia util.
"""
import re
from datetime import date, datetime, time

from regras import feriados
from regras.complexidade_entrega import horario_efetivo

CORTE_RODADA = time(14, 15)
A_PARTIR_DE = date(2026, 11, 3)
DIA_TODO = ("00:00", "23:59")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_saida(texto: str):
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime((texto or "").strip(), fmt).date()
        except ValueError:
            continue
    return None


def data_automatica(inicio_rodada: datetime, data_saida: str = "") -> date:
    """Proximo dia util (antes do corte) ou o seguinte (depois do corte)."""
    hoje = inicio_rodada.date()
    d1 = feriados.proximo_dia_util(hoje)
    if feriados.eh_dia_util(hoje) and inicio_rodada.time() <= CORTE_RODADA:
        data = d1
    else:
        data = feriados.proximo_dia_util(d1)
    saida = _parse_saida(data_saida)
    if saida and saida > data:
        data = feriados.rolar_para_dia_util(saida)
    return data


def _janela_valida(ini, fim) -> bool:
    """Dois "HH:MM" validos com inicio < fim (revisao final, 02/11: planilha,
    ajuste manual e mensagem nao garantem; a Vuupt recusaria fim < inicio e o
    pedido nunca seria criado)."""
    return (isinstance(ini, str) and isinstance(fim, str)
            and bool(_HHMM.match(ini)) and bool(_HHMM.match(fim)) and ini < fim)


def janela_automatica(horario_mensagem: dict | None, documento: str,
                      mapa_horarios: dict, ajustes_manuais: dict) -> tuple[str, str]:
    """Horario da mensagem (inicio/fim) vence; senao o do destinatario; senao
    o dia todo. Janela invalida (formato ou inicio >= fim) cai pra proxima."""
    if horario_mensagem:
        ini, fim = horario_mensagem.get("inicio"), horario_mensagem.get("fim")
        if _janela_valida(ini, fim):
            return ini, fim
    ini, fim = horario_efetivo(documento, mapa_horarios, ajustes_manuais)
    if _janela_valida(ini, fim):
        return ini, fim
    return DIA_TODO
