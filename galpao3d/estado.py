# -*- coding: utf-8 -*-
"""
galpao3d/estado.py

Estado unico que a tela 3D consulta a cada 60 s (placar, pessoas, filas, eventos,
rua, indicadores e sugestoes). So le as tabelas galpao_*; nao chama Stokki nem Vuupt.
"""
import json
from datetime import datetime

from galpao3d import indicadores, remanejamento
from galpao3d.banco import FMT, ultima_coleta
from galpao3d.turno import em_turno, inicio_dia_operacional

_TURNO_DA_AREA = {
    "recebimento": "recebimento_expedicao", "expedicao": "recebimento_expedicao",
    "recebimento_expedicao": "recebimento_expedicao", "estoque": "galpao",
    "separacao": "separacao_conferencia", "conferencia": "separacao_conferencia",
}
_SEM_MEDIA_PARADO_MIN = 45
_VERBO = {"Em Conferência": "separou", "Em separação": "separando"}


def _dt(valor: str) -> datetime:
    return datetime.strptime(valor, FMT)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _idade(conn, fonte: str, agora: datetime):
    t = ultima_coleta(conn, fonte, "ok")
    return None if t is None else max(0, int((agora - t).total_seconds() // 60))


def _ultimo_json(conn, tabela: str):
    row = conn.execute(f"SELECT json FROM {tabela} ORDER BY quando DESC LIMIT 1").fetchone()
    return json.loads(row["json"]) if row else None


def _acoes_do_dia(conn, desde: datetime) -> dict:
    """nome -> [(quando, texto da tarefa)] com eventos (usuario) e conferencias (inicio/fim) do dia."""
    acoes: dict = {}
    d = desde.strftime(FMT)
    for r in conn.execute("""
        SELECT e.usuario, e.situacao, e.quando, COALESCE(p.codigo, CAST(e.id_stokki AS TEXT)) AS codigo
        FROM galpao_eventos e LEFT JOIN galpao_pedidos p ON p.id_stokki = e.id_stokki
        WHERE e.quando >= ? AND e.usuario IS NOT NULL AND e.usuario <> ''
    """, (d,)):
        texto = f"{r['codigo']} · {_VERBO.get(r['situacao'], r['situacao'].lower())}"
        acoes.setdefault(r["usuario"], []).append((_dt(r["quando"]), texto))
    for r in conn.execute("""
        SELECT p.operador_tarefa AS usuario, e.situacao, e.quando,
               COALESCE(p.codigo, CAST(e.id_stokki AS TEXT)) AS codigo
        FROM galpao_eventos e JOIN galpao_pedidos p ON p.id_stokki = e.id_stokki
        WHERE e.quando >= ? AND p.operador_tarefa IS NOT NULL AND p.operador_tarefa <> ''
    """, (d,)):
        texto = f"{r['codigo']} · {_VERBO.get(r['situacao'], r['situacao'].lower())}"
        acoes.setdefault(r["usuario"], []).append((_dt(r["quando"]), texto))
    for r in conn.execute("""
        SELECT c.usuario, c.inicio, c.fim, COALESCE(p.codigo, CAST(c.id_stokki AS TEXT)) AS codigo
        FROM galpao_conferencias c LEFT JOIN galpao_pedidos p ON p.id_stokki = c.id_stokki
        WHERE c.inicio >= ? AND c.usuario IS NOT NULL AND c.usuario <> ''
    """, (d,)):
        acoes.setdefault(r["usuario"], []).append((_dt(r["inicio"]), f"{r['codigo']} · conferindo"))
        if r["fim"]:
            acoes[r["usuario"]].append((_dt(r["fim"]), f"{r['codigo']} · conferiu"))
    # o mesmo evento entra pela consulta do usuario e pela do operador da tarefa: conta uma vez so
    return {nome: sorted(set(lista)) for nome, lista in acoes.items()}


def _extras(conn, desde: datetime, equipe: dict, acoes: dict) -> dict:
    """Extras: so operador da tarefa (separacao) e conferente (conferencia); cliente que abre pedido fica fora."""
    extras: dict = {}
    d = desde.strftime(FMT)
    for r in conn.execute("""
        SELECT DISTINCT p.operador_tarefa AS nome FROM galpao_pedidos p
        JOIN galpao_eventos e ON e.id_stokki = p.id_stokki
        WHERE e.quando >= ? AND p.operador_tarefa IS NOT NULL AND p.operador_tarefa <> ''
    """, (d,)):
        extras[r["nome"]] = "separacao"
    for r in conn.execute("SELECT DISTINCT usuario FROM galpao_conferencias "
                          "WHERE inicio >= ? AND usuario IS NOT NULL AND usuario <> ''", (d,)):
        extras.setdefault(r["usuario"], "conferencia")
    return {n: a for n, a in extras.items() if n not in equipe}


def _status(area: str, acoes_pessoa: list, agora: datetime) -> str:
    if not em_turno(agora, _TURNO_DA_AREA.get(area, "galpao")):
        return "fora"
    if not acoes_pessoa:
        return "ok"
    marcas = sorted(a[0] for a in acoes_pessoa)
    intervalos = [(b - a).total_seconds() / 60 for a, b in zip(marcas, marcas[1:])]
    desde_ultimo = (agora - marcas[-1]).total_seconds() / 60
    if len(intervalos) < 2:
        return "parado" if desde_ultimo > _SEM_MEDIA_PARADO_MIN else "ok"
    media = sum(intervalos) / len(intervalos)
    if desde_ultimo > 3 * media:
        return "parado"
    if desde_ultimo > 1.5 * media:
        return "lento"
    return "ok"


def _pessoas(conn, equipe: dict, desde: datetime, agora: datetime) -> list:
    acoes = _acoes_do_dia(conn, desde)
    todas = [(n, a, False) for n, a in equipe.items()]
    todas += [(n, a, True) for n, a in _extras(conn, desde, equipe, acoes).items()]
    saida = []
    for nome, area, extra in todas:
        minhas = acoes.get(nome, [])
        tarefa = max(minhas, key=lambda x: x[0])[1] if minhas else ""
        saida.append({"nome": nome, "area": area, "tarefa": tarefa,
                      "status": _status(area, minhas, agora), "extra": extra})
    return saida


def _eventos(conn) -> list:
    linhas = conn.execute("SELECT id, id_stokki, situacao, usuario, quando FROM galpao_eventos "
                          "ORDER BY id DESC LIMIT 50").fetchall()
    saida = []
    for r in reversed(linhas):
        ant = conn.execute("SELECT situacao FROM galpao_eventos WHERE id_stokki = ? AND id < ? "
                           "ORDER BY id DESC LIMIT 1", (r["id_stokki"], r["id"])).fetchone()
        saida.append({"id": r["id"], "quando": _iso(_dt(r["quando"])), "usuario": r["usuario"],
                      "id_stokki": r["id_stokki"], "de": ant["situacao"] if ant else None,
                      "para": r["situacao"]})
    return saida


def montar_estado(conn, config: dict, agora: datetime | None = None) -> dict:
    agora = agora or datetime.now()
    desde = inicio_dia_operacional(agora)
    cfg_g = (config or {}).get("galpao_3d", {}) or {}
    equipe = cfg_g.get("equipe", {}) or {}

    cont = _ultimo_json(conn, "galpao_contagens")
    rua = _ultimo_json(conn, "galpao_rua")
    c = cont or {}
    placar = {
        "recebendo": None, "estoque": None,
        "separacao": c.get("count_separating"),
        "conferencia": c.get("count_pack"),
        "expedicao": c.get("count_waiting_carrier"),
        "na_rua": rua.get("carros_na_rua") if rua else None,
        "entregues": rua.get("entregues") if rua else None,
        "voltando": rua.get("insucessos") if rua else None,
    }
    sep_conf = conn.execute("""
        SELECT COUNT(*) FROM galpao_pedidos p
        WHERE p.situacao = 'Em Conferência'
          AND NOT EXISTS (SELECT 1 FROM galpao_conferencias c
                          WHERE c.id_stokki = p.id_stokki AND c.inicio IS NOT NULL)
    """).fetchone()[0]
    filas = {"rec_estoque": None, "estoque_sep": None, "sep_conf": sep_conf,
             "conf_exp": c.get("count_waiting_carrier")}

    pessoas = _pessoas(conn, equipe, desde, agora)
    por_area: dict = {}
    for p in pessoas:
        if p["status"] != "fora":
            por_area[p["area"]] = por_area.get(p["area"], 0) + 1

    esperas = indicadores.esperas(conn, desde, agora)
    sugestoes = remanejamento.sugerir(
        {"separacao": c.get("count_open"), "conferencia": sep_conf, "estoque": None},
        esperas, por_area, cfg_g.get("remanejamento", {}), agora)

    idade_s = _idade(conn, "stokki", agora)
    nivel = "vermelho" if idade_s is None or idade_s > 15 else ("amarelo" if idade_s > 5 else "ok")
    return {
        "agora": _iso(agora), "dia_desde": _iso(desde),
        "idade": {"stokki_min": idade_s, "vuupt_min": _idade(conn, "vuupt", agora), "stokki_nivel": nivel},
        "placar": placar, "filas": filas, "pessoas": pessoas, "eventos": _eventos(conn),
        "rua": rua,
        "indicadores": {
            "conferencia_geral": indicadores.conferencia_geral(conn, desde),
            "conferencia_por_pessoa": indicadores.conferencia_por_pessoa(conn, desde),
            "produtividade_separacao": indicadores.produtividade_separacao(conn, desde, agora),
        },
        "sugestoes": sugestoes,
    }
