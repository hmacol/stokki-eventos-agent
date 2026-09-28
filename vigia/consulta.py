# -*- coding: utf-8 -*-
"""
vigia/consulta.py

Leitura do vigia pra Torre (exceções agrupadas por estado) e pra tela
/vigia (lista pedido a pedido). Só lê vigia_pedidos -- quem calcula é
vigia/vigiar.py (timer de 15 min).
"""
import sqlite3
from datetime import datetime
from pathlib import Path

from vigia import banco, regras


def _idade(desde: str | None, agora: datetime) -> str:
    try:
        delta = agora - datetime.strptime(desde or "", banco.FMT)
    except ValueError:
        return "—"
    horas = int(delta.total_seconds() // 3600)
    if horas < 1:
        return f"{int(delta.total_seconds() // 60)} min"
    if horas < 48:
        return f"{horas}h"
    return f"{horas // 24}d {horas % 24}h"


def listar(estado: str = "", so_vencidos: bool = False, busca: str = "",
           db_path: Path | None = None, agora: datetime | None = None) -> dict:
    agora = agora or datetime.now()
    conn = banco.conectar(db_path)
    try:
        linhas = [dict(r) for r in conn.execute("SELECT * FROM vigia_pedidos")]
        ultima_rodada = max((l["visto_em"] for l in linhas), default=None)
        ultima_listagem = banco.ultima_listagem_completa(conn)
    finally:
        conn.close()

    resumo = {e: {"estado": e, "rotulo": regras.ROTULOS[e], "total": 0, "vencidos": 0,
                  "critico": e in regras.CRITICOS} for e in regras.ORDEM}
    for l in linhas:
        if l["estado"] in resumo:
            resumo[l["estado"]]["total"] += 1
            resumo[l["estado"]]["vencidos"] += l["vencido"]

    termo = busca.strip().upper()
    filtradas = [
        l for l in linhas
        if (not estado or l["estado"] == estado)
        and (not so_vencidos or l["vencido"])
        and (not termo or termo in l["codigo"] or termo in (l["detalhe"] or "").upper()
             or termo in (l["motivo"] or "").upper())
    ]
    ordem = {e: i for i, e in enumerate(regras.ORDEM)}
    filtradas.sort(key=lambda l: (-l["vencido"], ordem.get(l["estado"], 99), l["desde"]))
    for l in filtradas:
        l["rotulo"] = regras.ROTULOS.get(l["estado"], l["estado"])
        l["idade"] = _idade(l["desde"], agora)
        l["critico"] = l["estado"] in regras.CRITICOS
    return {
        "linhas": filtradas,
        "resumo": [r for r in resumo.values() if r["total"]],
        "total": len(linhas),
        "vencidos": sum(l["vencido"] for l in linhas),
        "ultima_rodada": ultima_rodada,
        "ultima_listagem_stokki": ultima_listagem,
    }


def excecoes_torre(data_iso: str, db_path: Path | None = None, agora: datetime | None = None) -> list[dict]:
    """Uma exceção por estado com prazo vencido (formato de
    torre_controle._montar_excecoes, sem o _epoch). O id leva a data e a
    quantidade: "Tratar" esconde só por hoje e volta se o número mudar."""
    agora = agora or datetime.now()
    try:
        conn = banco.conectar(db_path)
    except sqlite3.Error:
        return []
    try:
        linhas = [dict(r) for r in conn.execute(
            "SELECT codigo, estado, desde FROM vigia_pedidos WHERE vencido = 1 ORDER BY desde")]
    finally:
        conn.close()
    por_estado: dict[str, list[dict]] = {}
    for l in linhas:
        por_estado.setdefault(l["estado"], []).append(l)
    excecoes = []
    for estado in regras.ORDEM:
        itens = por_estado.get(estado)
        if not itens:
            continue
        mais_antigo = itens[0]
        exemplos = ", ".join(i["codigo"] for i in itens[:5])
        excecoes.append({
            "id": f"vigia:{estado}:{data_iso}:{len(itens)}",
            "severidade": "critico" if estado in regras.CRITICOS else "atencao",
            "tipo": "Vigia",
            "descricao": (f"{len(itens)} pedido(s) — {regras.ROTULOS[estado].lower()} com prazo vencido "
                          f"(mais antigo: {mais_antigo['codigo']}, há {_idade(mais_antigo['desde'], agora)}). "
                          f"Ex.: {exemplos}."),
            "quando": None,
            "acao": {"tipo": "link", "url": f"/vigia?estado={estado}&vencidos=1", "rotulo": "Ver pedidos"},
        })
    return excecoes
