# -*- coding: utf-8 -*-
"""
batimento/consulta.py

Leitura do batimento pra aba Fechamento do /vigia, pra Torre e pro e-mail
do bater.py (spec docs/superpowers/specs/2026-10-05-batimento-saida-design.md).
So le batimento_pedidos/batimento_rodadas, exceto tratar().
"""
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from batimento import banco, regras
from regras import feriados

RODADAS_NO_HISTORICO = 14
HORAS_RODADA_ATRASADA = 26   # o timer roda todo dia as 07h25, inclusive fim de semana


def atrasada(ultima: dict | None, agora: datetime) -> bool:
    """Ultima rodada com mais de 26h = timer parado ou falhando."""
    if not ultima:
        return False
    try:
        quando = datetime.strptime(ultima["rodada_em"], banco.FMT)
    except (KeyError, TypeError, ValueError):
        return False
    return agora - quando > timedelta(hours=HORAS_RODADA_ATRASADA)


def _proximo_dia_util(dt: datetime) -> datetime:
    d = dt + timedelta(days=1)
    while not feriados.eh_dia_util(d):   # fim de semana e feriado (regras/feriados.py)
        d += timedelta(days=1)
    return d


def vencida(desde: str | None, agora: datetime) -> bool:
    """Passou 1 dia util desde `desde` (mesmo horario do proximo dia de
    segunda a sexta)."""
    try:
        dt = datetime.strptime(desde or "", banco.FMT)
    except ValueError:
        return False
    return agora >= _proximo_dia_util(dt)


def _rodada(row) -> dict:
    d = dict(row)
    try:
        d["resumo"] = json.loads(d.get("resumo_json") or "{}")
    except ValueError:
        d["resumo"] = {}
    return d


def ultima_rodada(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM batimento_rodadas ORDER BY id DESC LIMIT 1").fetchone()
    return _rodada(row) if row else None


def _linha(row, agora: datetime) -> dict:
    l = dict(row)
    l["motivo_txt"] = regras.ROTULOS_DIVERGENCIA.get(l["rotulo"], l["rotulo"])
    l["real"] = l["rotulo"] in regras.DIVERGENCIAS_REAIS
    # div_desde nao zera quando o pedido oscila e volta ao mesmo motivo
    l["div_desde"] = l.get("div_desde") or l["desde"]
    l["vencida"] = vencida(l["div_desde"], agora)
    l["tratada"] = bool(l.get("tratado_em"))
    try:
        l["evidencias"] = [e for e in json.loads(l.get("evidencias_json") or "[]") if e]
    except ValueError:
        l["evidencias"] = []
    return l


def fechamento(motivo: str = "", so_reais: bool = False, incluir_tratadas: bool = False, busca: str = "",
               db_path: Path | None = None, agora: datetime | None = None) -> dict:
    agora = agora or datetime.now()
    conn = banco.conectar(db_path)
    try:
        todas = [_linha(r, agora) for r in conn.execute(
            "SELECT * FROM batimento_pedidos WHERE caixa = 'DIVERGENCIA' "
            "ORDER BY COALESCE(div_desde, desde), codigo")]
        ultima = ultima_rodada(conn)
        rodadas = [_rodada(r) for r in conn.execute(
            "SELECT * FROM batimento_rodadas ORDER BY id DESC LIMIT ?", (RODADAS_NO_HISTORICO,))]
    finally:
        conn.close()

    abertas = [l for l in todas if not l["tratada"]]
    contagem: dict[str, int] = {}
    for l in abertas:
        contagem[l["rotulo"]] = contagem.get(l["rotulo"], 0) + 1
    por_motivo = sorted(
        ({"motivo": m, "rotulo": regras.ROTULOS_DIVERGENCIA.get(m, m), "total": n,
          "real": m in regras.DIVERGENCIAS_REAIS} for m, n in contagem.items()),
        key=lambda x: (not x["real"], -x["total"], x["motivo"]))

    termo = busca.strip().upper()
    linhas = [
        l for l in todas
        if (incluir_tratadas or not l["tratada"])
        and (not motivo or l["rotulo"] == motivo)
        and (not so_reais or l["real"])
        and (not termo or termo in l["codigo"] or termo in (l["embarcador"] or "").upper()
             or termo in (l["transportadora"] or "").upper())
    ]
    return {"linhas": linhas, "por_motivo": por_motivo, "total_abertas": len(abertas),
            "ultima": ultima, "rodadas": rodadas, "atrasada": atrasada(ultima, agora)}


def novidades(conn: sqlite3.Connection, rodada_em: str) -> list[dict]:
    """Divergencias que entraram num motivo NOVO nesta rodada. Voltar ao
    mesmo motivo depois de oscilar nao e novidade (div_desde nao muda)."""
    agora = datetime.strptime(rodada_em, banco.FMT)
    return [_linha(r, agora) for r in conn.execute(
        "SELECT * FROM batimento_pedidos WHERE caixa = 'DIVERGENCIA' AND div_desde = ? AND visto_em = ? "
        "ORDER BY codigo", (rodada_em, rodada_em))]


def excecoes_torre(data_iso: str, db_path: Path | None = None, agora: datetime | None = None) -> list[dict]:
    """Formato de torre_controle._montar_excecoes (sem _epoch). Um item por
    divergencia REAL nao tratada ha mais de 1 dia util (id estavel por
    pedido+motivo: "Tratar" na Torre esconde ate o motivo mudar) e um
    critico se a ultima rodada nao fechou."""
    agora = agora or datetime.now()
    try:
        conn = banco.conectar(db_path)
    except sqlite3.Error:
        return []
    try:
        ultima = ultima_rodada(conn)
        if ultima is None:
            return []
        marcas = ",".join("?" * len(regras.DIVERGENCIAS_REAIS))
        rows = conn.execute(
            f"SELECT codigo, embarcador, rotulo, COALESCE(div_desde, desde) AS desde FROM batimento_pedidos "
            f"WHERE caixa = 'DIVERGENCIA' AND tratado_em IS NULL AND rotulo IN ({marcas}) "
            f"ORDER BY COALESCE(div_desde, desde), codigo",
            regras.DIVERGENCIAS_REAIS).fetchall()
    finally:
        conn.close()

    itens = []
    if atrasada(ultima, agora):
        quando = datetime.strptime(ultima["rodada_em"], banco.FMT).strftime("%d/%m %H:%M")
        itens.append({
            "id": f"batimento:sem-rodada:{ultima['rodada_em']}",
            "severidade": "critico", "tipo": "Batimento",
            "descricao": f"Batimento sem rodada desde {quando}: o job das 07h25 não rodou ou falhou.",
            "quando": None,
            "acao": {"tipo": "link", "url": "/vigia?aba=fechamento", "rotulo": "Ver no Fechamento"},
        })
    if not ultima["fecha"]:
        itens.append({
            "id": f"batimento:nao-fecha:{ultima['rodada_em']}",
            "severidade": "critico", "tipo": "Batimento",
            "descricao": (f"Batimento de {ultima['rodada_em'][:16]} não fecha: {ultima['lancados']} lançados "
                          f"≠ {ultima['destino']} destino + {ultima['em_andamento']} andamento + "
                          f"{ultima['divergencia']} divergência."),
            "quando": None,
            "acao": {"tipo": "link", "url": "/vigia?aba=fechamento", "rotulo": "Ver no Fechamento"},
        })
    for r in rows:
        if not vencida(r["desde"], agora):
            continue
        desde = datetime.strptime(r["desde"], banco.FMT).strftime("%d/%m")
        itens.append({
            "id": f"batimento:{r['codigo']}:{r['rotulo']}",
            "severidade": "atencao", "tipo": "Batimento",
            "descricao": (f"{r['codigo']} {r['embarcador'] or ''}: "
                          f"{regras.ROTULOS_DIVERGENCIA.get(r['rotulo'], r['rotulo'])} desde {desde}").replace("  ", " "),
            "quando": None,
            "acao": {"tipo": "link", "url": f"/vigia?aba=fechamento&busca={r['codigo']}",
                     "rotulo": "Ver no Fechamento"},
        })
    return itens


def tratar(codigo: str, por: str, obs: str, db_path: Path | None = None) -> None:
    conn = banco.conectar(db_path)
    try:
        banco.marcar_tratado(conn, codigo, por, obs)
    finally:
        conn.close()
