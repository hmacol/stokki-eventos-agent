# -*- coding: utf-8 -*-
"""
nucleo/tempos.py

Durações por parada (Hugo, 26/08: "salvar o tempo entre o arrived e o
completed pra saber quanto estão demorando pra receber"):

    tempo_deslocamento_s = arrived_at  - started_at    (saiu -> chegou)
    tempo_no_local_s     = completed_at - arrived_at   (chegou -> recebeu/insucesso)

Gravadas em nucleo_paradas a cada evento (app) e a cada sync (VUUPT), e
recalculáveis a qualquer momento (`recalcular_todas`). Os timestamps
chegam em dois formatos: "YYYY-MM-DD HH:MM:SS" (app/VUUPT) e ISO com "T"
(alguns campos da VUUPT) -- `parse_ts` aceita os dois e ignora fuso.

Negativo (relógio do aparelho fora de ordem, confirmação em lote da
VUUPT) vira NULL, nunca um número absurdo -- a média de
nucleo/metricas.py só considera valores plausíveis.
"""
import sqlite3
from datetime import datetime

_FORMATOS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M")


def parse_ts(valor: str | None) -> datetime | None:
    if not valor:
        return None
    txt = str(valor).strip()
    # descarta fuso/fração: "2026-08-26T09:10:00.000000Z", "+00:00", "-03:00"
    for sep in ("Z", "+"):
        if sep in txt[10:]:
            txt = txt[:10] + txt[10:].split(sep)[0]
    if "-" in txt[11:]:
        txt = txt[:11] + txt[11:].split("-")[0]
    txt = txt.split(".")[0]
    for fmt in _FORMATOS:
        try:
            return datetime.strptime(txt, fmt)
        except ValueError:
            continue
    return None


def diferenca_s(inicio: str | None, fim: str | None) -> int | None:
    """Segundos entre dois carimbos; None se faltar um deles ou se a
    diferença for negativa (dado fora de ordem)."""
    a, b = parse_ts(inicio), parse_ts(fim)
    if a is None or b is None:
        return None
    seg = int((b - a).total_seconds())
    return seg if seg >= 0 else None


def calcular_tempos(started_at, arrived_at, completed_at) -> tuple[int | None, int | None]:
    return diferenca_s(started_at, arrived_at), diferenca_s(arrived_at, completed_at)


def atualizar_tempos_parada(conn: sqlite3.Connection, parada_id: int) -> tuple[int | None, int | None]:
    row = conn.execute("SELECT started_at, arrived_at, completed_at FROM nucleo_paradas WHERE id = ?", (parada_id,)).fetchone()
    if not row:
        return None, None
    desloc, local = calcular_tempos(row[0], row[1], row[2])
    conn.execute("UPDATE nucleo_paradas SET tempo_deslocamento_s = ?, tempo_no_local_s = ? WHERE id = ?",
                 (desloc, local, parada_id))
    return desloc, local


def recalcular_todas(conn: sqlite3.Connection) -> int:
    """Backfill: recalcula as duas durações em todas as paradas que têm
    pelo menos arrived_at (as demais ficam NULL). Retorna quantas mudaram."""
    n = 0
    for r in conn.execute("SELECT id, started_at, arrived_at, completed_at, tempo_deslocamento_s, tempo_no_local_s FROM nucleo_paradas WHERE arrived_at IS NOT NULL OR started_at IS NOT NULL").fetchall():
        desloc, local = calcular_tempos(r[1], r[2], r[3])
        if (desloc, local) != (r[4], r[5]):
            conn.execute("UPDATE nucleo_paradas SET tempo_deslocamento_s = ?, tempo_no_local_s = ? WHERE id = ?", (desloc, local, r[0]))
            n += 1
    conn.commit()
    return n
