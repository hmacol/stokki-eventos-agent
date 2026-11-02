# -*- coding: utf-8 -*-
"""
Regra pura do agendamento automatico (sem rede). Spec:
docs/superpowers/specs/2026-11-02-agendamento-automatico-proximo-dia-util-design.md
Decisao do Hugo (02/11, opcao A): o corte das 14h vale de verdade; a rodada
das 14h05 ainda conta como antes do corte (compara-se a hora de INICIO da
rodada com CORTE_RODADA). `data_saida` (expedition_date da Stokki) e data
minima: se for depois da data do corte, vale ela, rolada pra dia util.
"""
from datetime import date, datetime, time

from regras import feriados
from regras.complexidade_entrega import horario_efetivo

CORTE_RODADA = time(14, 15)
A_PARTIR_DE = date(2026, 11, 3)
DIA_TODO = ("00:00", "23:59")


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


def janela_automatica(horario_mensagem: dict | None, documento: str,
                      mapa_horarios: dict, ajustes_manuais: dict) -> tuple[str, str]:
    """Horario da mensagem (inicio/fim) vence; senao o do destinatario."""
    if horario_mensagem and horario_mensagem.get("inicio") and horario_mensagem.get("fim"):
        return horario_mensagem["inicio"], horario_mensagem["fim"]
    return horario_efetivo(documento, mapa_horarios, ajustes_manuais)
