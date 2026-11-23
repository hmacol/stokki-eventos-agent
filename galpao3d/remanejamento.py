# -*- coding: utf-8 -*-
"""
galpao3d/remanejamento.py

Regra que SUGERE (nunca executa) mover uma pessoa entre etapas vizinhas do galpao.
"""
from datetime import datetime
from statistics import median

from galpao3d.turno import em_turno

PADRAO = {"fila_min": 10, "espera_min": 60, "folga_max": 3}

# etapa que recebe -> etapas vizinhas que podem ceder gente
_VIZINHAS = {
    "separacao": ["conferencia", "estoque"],
    "conferencia": ["separacao"],
    "estoque": ["separacao"],
}
_NOME = {"separacao": "separação", "conferencia": "conferência", "estoque": "estoque"}
_TURNO_DA_ETAPA = {"separacao": "separacao_conferencia", "conferencia": "separacao_conferencia",
                   "estoque": "galpao"}


def _hhmm(minutos: float) -> str:
    m = int(round(minutos))
    return f"{m // 60}h{m % 60:02d}"


def sugerir(filas: dict, esperas: list, pessoas: dict, cfg: dict, agora: datetime) -> list[dict]:
    """Sugere mover 1 pessoa de A para B (vizinhas) quando B esta travada e A esta folgada.

    filas: etapa -> pedidos esperando para ENTRAR nela (None = sem dado: nunca sugere).
    esperas: saida de indicadores.esperas; a mediana de B usa as etapas que terminam em B.
    pessoas: area -> pessoas em turno agora.
    agora: decide "fora do turno" (separacao/conferencia: turno separacao_conferencia;
           estoque: turno galpao). Fora do turno de A ou de B nao ha sugestao.
    """
    fila_min = (cfg or {}).get("fila_min", PADRAO["fila_min"])
    espera_min = (cfg or {}).get("espera_min", PADRAO["espera_min"])
    folga_max = (cfg or {}).get("folga_max", PADRAO["folga_max"])
    sugestoes = []
    for b, vizinhas in _VIZINHAS.items():
        fila_b = filas.get(b)
        if fila_b is None or fila_b < fila_min:
            continue
        tempos = [e["minutos"] for e in esperas if e["etapa"].endswith(b)]
        if not tempos:
            continue
        mediana = median(tempos)
        if mediana < espera_min or not em_turno(agora, _TURNO_DA_ETAPA[b]):
            continue
        for a in vizinhas:
            fila_a = filas.get(a)
            if fila_a is None or fila_a > folga_max:
                continue
            if (pessoas.get(a) or 0) < 1 or not em_turno(agora, _TURNO_DA_ETAPA[a]):
                continue
            sugestoes.append({
                "de": a, "para": b,
                "motivo": (f"{fila_b} pedidos esperando {_NOME[b]}, mediana {_hhmm(mediana)}; "
                           f"{_NOME[a]} com {fila_a} na fila"),
            })
    return sugestoes
