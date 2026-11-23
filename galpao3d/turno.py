"""
turno.py

Turnos da operacao e minutos uteis (Galpao 3D).

Noite e fim de semana nao contam: espera calculada so soma a parte que
cai dentro da janela do turno em dia util (regras.feriados.eh_dia_util).
"""
from datetime import datetime, time, timedelta

from regras.feriados import eh_dia_util

# turno -> (hora de inicio, hora de fim); fim e exclusivo
TURNOS: dict[str, tuple[int, int]] = {
    "recebimento_expedicao": (4, 14),
    "separacao_conferencia": (8, 18),
    "galpao": (4, 18),
}

_COLETA_INICIO = time(3, 50)
_COLETA_FIM = time(18, 30)


def inicio_dia_operacional(agora: datetime) -> datetime:
    """As 4h de hoje se agora ja passou das 4h; senao as 4h de ontem."""
    base = datetime.combine(agora.date(), time(4))
    if agora < base:
        base -= timedelta(days=1)
    return base


def minutos_uteis(inicio: datetime, fim: datetime, turno: str) -> int:
    """Minutos entre inicio e fim contando so a janela do turno em dia util."""
    if fim <= inicio:
        return 0
    h_ini, h_fim = TURNOS[turno]
    total = 0
    dia = inicio.date()
    while dia <= fim.date():
        if eh_dia_util(dia):
            janela_ini = datetime.combine(dia, time(h_ini))
            janela_fim = datetime.combine(dia, time(h_fim))
            a = max(inicio, janela_ini)
            b = min(fim, janela_fim)
            if b > a:
                total += int((b - a).total_seconds() // 60)
        dia += timedelta(days=1)
    return total


def em_turno(agora: datetime, turno: str) -> bool:
    """Verdadeiro em dia util, dentro de [inicio, fim) do turno."""
    if not eh_dia_util(agora):
        return False
    h_ini, h_fim = TURNOS[turno]
    return time(h_ini) <= agora.time() < time(h_fim)


def deve_coletar(agora: datetime) -> bool:
    """Dia util: coleta sempre entre 03:50 e 18:30; fora disso, so a cada 15 min.
    Dia sem turno (fim de semana/feriado): so a cada 15 min, o dia todo."""
    if eh_dia_util(agora) and _COLETA_INICIO <= agora.time() <= _COLETA_FIM:
        return True
    return agora.minute % 15 == 0
