# -*- coding: utf-8 -*-
"""
galpao3d/indicadores.py

Indicadores do Galpao 3D lidos das tabelas galpao_* (funcoes puras sobre a conexao).

- Conferencia: media de segundos ponderada pelas unidades do pedido (pedido grande
  demora mais; pedido sem unidades pesa 1).
- Separacao: unidades por hora do operador. O tempo por pedido nao serve (a Stokki
  separa em ondas), entao usa-se o intervalo entre o 1o e o ultimo pedido que o
  operador mandou pra conferencia, em minutos uteis, com minimo de 30 min.
- Espera: minutos uteis entre a chegada na conferencia e o inicio dela.
"""
from datetime import datetime

from galpao3d.banco import FMT
from galpao3d.turno import minutos_uteis

_TURNO = "separacao_conferencia"
_MIN_HORAS = 0.5


def _txt(dt: datetime) -> str:
    return dt.strftime(FMT)


def _dt(valor: str) -> datetime:
    return datetime.strptime(valor, FMT)


def conferencia_por_pessoa(conn, desde: datetime) -> list[dict]:
    linhas = conn.execute("""
        SELECT c.usuario, c.segundos, COALESCE(NULLIF(p.unidades, 0), 1) AS peso
        FROM galpao_conferencias c
        LEFT JOIN galpao_pedidos p ON p.id_stokki = c.id_stokki
        WHERE c.inicio >= ? AND c.segundos IS NOT NULL AND c.usuario IS NOT NULL
    """, (_txt(desde),)).fetchall()
    acc: dict[str, list] = {}
    for r in linhas:
        a = acc.setdefault(r["usuario"], [0, 0, 0])  # pedidos, unidades, soma seg*peso
        a[0] += 1
        a[1] += r["peso"]
        a[2] += r["segundos"] * r["peso"]
    saida = [{"usuario": u, "pedidos": a[0], "unidades": a[1], "media_s_ponderada": a[2] / a[1]}
             for u, a in acc.items()]
    return sorted(saida, key=lambda x: x["usuario"])


def conferencia_geral(conn, desde: datetime) -> dict:
    por_pessoa = conferencia_por_pessoa(conn, desde)
    unidades = sum(p["unidades"] for p in por_pessoa)
    if not unidades:
        return {"pedidos": 0, "media_s_ponderada": 0}
    soma = sum(p["media_s_ponderada"] * p["unidades"] for p in por_pessoa)
    return {"pedidos": sum(p["pedidos"] for p in por_pessoa), "media_s_ponderada": soma / unidades}


def _chegadas_na_conferencia(conn, desde: datetime, agora: datetime):
    """1o evento 'Em Conferencia' de cada pedido dentro do dia: (id_stokki, usuario, quando)."""
    linhas = conn.execute("""
        SELECT id_stokki, usuario, MIN(quando) AS quando
        FROM galpao_eventos
        WHERE situacao = 'Em Conferência'
        GROUP BY id_stokki
        HAVING MIN(quando) >= ? AND MIN(quando) <= ?
    """, (_txt(desde), _txt(agora))).fetchall()
    return [(r["id_stokki"], r["usuario"], _dt(r["quando"])) for r in linhas]


def produtividade_separacao(conn, desde: datetime, agora: datetime) -> list[dict]:
    acc: dict[str, dict] = {}
    for id_stokki, usuario, quando in _chegadas_na_conferencia(conn, desde, agora):
        row = conn.execute("SELECT unidades, operador_tarefa FROM galpao_pedidos WHERE id_stokki = ?",
                           (id_stokki,)).fetchone()
        # separador = "Operador da Tarefa"; o usuario do evento so entra se ele faltar.
        # Pedido sem nenhum dos dois e ignorado.
        operador = ((row["operador_tarefa"] if row else None) or "").strip() or usuario
        if not operador:
            continue
        a = acc.setdefault(operador, {"pedidos": 0, "unidades": 0, "ini": quando, "fim": quando})
        a["pedidos"] += 1
        a["unidades"] += (row["unidades"] or 0) if row else 0
        a["ini"] = min(a["ini"], quando)
        a["fim"] = max(a["fim"], quando)
    saida = []
    for operador, a in acc.items():
        horas = max(minutos_uteis(a["ini"], a["fim"], _TURNO) / 60, _MIN_HORAS)
        saida.append({"operador": operador, "pedidos": a["pedidos"], "unidades": a["unidades"],
                      "horas": horas, "unidades_por_hora": a["unidades"] / horas})
    return sorted(saida, key=lambda x: x["operador"])


def esperas(conn, desde: datetime, agora: datetime) -> list[dict]:
    """Espera de cada pedido entre chegar na conferencia e ela comecar (ou ate agora).

    Pedido AINDA esperando (situacao atual 'Em Conferencia' sem inicio de conferencia)
    entra sempre, mesmo que tenha chegado antes de `desde` (chegou ontem 17:50 e
    continua parado de manha). Pedido cuja conferencia ja comecou entra se ela
    comecou desde `desde`."""
    linhas = conn.execute("""
        SELECT e.id_stokki, MIN(e.quando) AS chegada, p.situacao, c.inicio
        FROM galpao_eventos e
        LEFT JOIN galpao_pedidos p ON p.id_stokki = e.id_stokki
        LEFT JOIN galpao_conferencias c ON c.id_stokki = e.id_stokki
        WHERE e.situacao = 'Em Conferência'
        GROUP BY e.id_stokki
        HAVING MIN(e.quando) <= ?
    """, (_txt(agora),)).fetchall()
    saida = []
    for r in linhas:
        if r["inicio"]:
            fim = _dt(r["inicio"])
            if fim < desde:
                continue
        elif r["situacao"] == "Em Conferência":
            fim = agora
        else:
            continue   # saiu da conferencia sem inicio conhecido: nao ha espera a medir
        saida.append({"id_stokki": r["id_stokki"], "etapa": "separacao→conferencia",
                      "minutos": minutos_uteis(_dt(r["chegada"]), fim, _TURNO)})
    return saida
