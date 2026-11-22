# -*- coding: utf-8 -*-
"""
vigia/consulta.py

Leitura do vigia pra Torre (exceções agrupadas por estado) e pra tela
/vigia (lista pedido a pedido). Só lê vigia_pedidos -- quem calcula é
vigia/vigiar.py (timer de 15 min).
"""
import sqlite3
from datetime import date, datetime
from pathlib import Path

from regras import feriados
from vigia import banco, regras

HORAS_RETRATO_VALIDO = 30  # mesmo limite de vigia/vigiar.py


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
        l["acao"], l["dono"] = regras.acao_e_dono(l["estado"], l.get("motivo"))
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
            "SELECT codigo, estado, desde, motivo FROM vigia_pedidos WHERE vencido = 1 ORDER BY desde")]
        ultima_listagem = banco.ultima_listagem_completa(conn)
        ultima_rodada = conn.execute("SELECT MAX(visto_em) FROM vigia_pedidos").fetchone()[0]
    finally:
        conn.close()
    por_estado: dict[str, list[dict]] = {}
    for l in linhas:
        por_estado.setdefault(l["estado"], []).append(l)
    excecoes = []
    # O vigia depende do retrato da Stokki que o pipeline grava; sem ele,
    # "sem serviço" e insucesso ficam congelados. Avisa em vez de calar.
    if ultima_rodada or ultima_listagem:
        try:
            idade_h = (agora - datetime.strptime(ultima_listagem, banco.FMT)).total_seconds() / 3600 \
                if ultima_listagem else None
        except ValueError:
            idade_h = None
        if idade_h is None or idade_h > HORAS_RETRATO_VALIDO:
            excecoes.append({
                "id": f"vigia:retrato:{data_iso}",
                "severidade": "critico",
                "tipo": "Vigia",
                "descricao": ("Vigia sem retrato recente da Stokki (última listagem completa do pipeline: "
                              f"{ultima_listagem or 'nenhuma'}) -- 'sem serviço' e insucessos não estão "
                              "sendo atualizados. Conferir se o pipeline está rodando."),
                "quando": None,
                "acao": {"tipo": "link", "url": "/vigia", "rotulo": "Abrir vigia"},
            })
    # Uma linha por EQUIPE (Hugo, 21/11): o que esta vencido e ninguem decidiu.
    # Pedido com decisao ainda valendo ("Tratar" com data) nao conta.
    decididos = _decisoes_ativas_ou_vazio(db_path, agora.date())
    por_dono: dict[str, list[dict]] = {}
    for estado in regras.ORDEM:
        for i in por_estado.get(estado, []):
            if decididos.get(i["codigo"], {}).get("estado") == estado:
                continue
            _, dono = regras.acao_e_dono(estado, i.get("motivo"))
            por_dono.setdefault(dono, []).append({**i, "estado": estado})
    for dono in EQUIPES:
        itens = por_dono.get(dono)
        if not itens:
            continue
        itens.sort(key=lambda i: i["desde"])
        mais_antigo = itens[0]
        quebra: dict[str, int] = {}
        for i in itens:
            quebra[i["estado"]] = quebra.get(i["estado"], 0) + 1
        resumo = ", ".join(f"{n} {regras.ROTULOS[e][0].lower() + regras.ROTULOS[e][1:]}" for e, n in quebra.items())
        excecoes.append({
            # quantidade + mais antigo no id: "Tratar" esconde so esta
            # situacao -- pedido novo no lugar de outro faz reaparecer.
            "id": f"vigia:equipe:{dono}:{data_iso}:{len(itens)}:{mais_antigo['codigo']}",
            "severidade": "critico" if any(i["estado"] in regras.CRITICOS for i in itens) else "atencao",
            "tipo": "Vigia",
            "descricao": (f"{dono}: {len(itens)} pedido(s) vencido(s) sem decisão — {resumo}. "
                          f"Mais antigo: {mais_antigo['codigo']}, há {_idade(mais_antigo['desde'], agora)}."),
            "quando": None,
            "acao": {"tipo": "link", "url": f"/vigia?equipe={dono}", "rotulo": "Ver pedidos"},
        })
    return excecoes


# ── Lista de ação (aba "Não entregues" da /vigia, Hugo 21/11) ───────────────

EQUIPES = (regras.LOGISTICA, regras.ATENDIMENTO, regras.EXPEDICAO)
NIVEL_PARA_EQUIPE = {"logistica": regras.LOGISTICA, "atendimento": regras.ATENDIMENTO}


def equipe_do_nivel(nivel: str | None) -> str:
    """Equipe que a tela abre filtrando pra esse nível de acesso ('' = todas)."""
    return NIVEL_PARA_EQUIPE.get(nivel or "", "")


def _dias(desde: str | None, agora: datetime) -> int:
    try:
        return max(0, (agora - datetime.strptime(desde or "", banco.FMT)).days)
    except ValueError:
        return 0


def _decisoes_ativas_ou_vazio(db_path: Path | None, hoje: date) -> dict[str, dict]:
    """codigo -> decisão que ainda vale hoje ({estado, ate, ...}); nunca levanta."""
    try:
        conn = banco.conectar(db_path)
    except sqlite3.Error:
        return {}
    try:
        return {r["codigo"]: dict(r) for r in conn.execute(
            "SELECT * FROM vigia_decisoes WHERE ate >= ?", (hoje.isoformat(),))}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _tabela_existe(conn: sqlite3.Connection, nome: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                             (nome,)).fetchone())


def _tem_coluna(conn: sqlite3.Connection, tabela: str, coluna: str) -> bool:
    return any(r[1] == coluna for r in conn.execute(f"PRAGMA table_info({tabela})"))


def _entregues_nao_expedidos(conn: sqlite3.Connection) -> list[dict]:
    """Divergência ENTREGUE_NAO_EXPEDIDO do batimento, ainda sem tratativa lá,
    no formato das linhas do vigia (vence na hora: já foi entregue)."""
    if not _tabela_existe(conn, "batimento_pedidos"):
        return []
    desde = "COALESCE(div_desde, desde)" if _tem_coluna(conn, "batimento_pedidos", "div_desde") else "desde"
    saida = []
    for r in conn.execute(f"""
            SELECT codigo, embarcador, {desde} AS desde FROM batimento_pedidos
            WHERE caixa = 'DIVERGENCIA' AND rotulo = ? AND tratado_em IS NULL""",
                          (regras.ENTREGUE_NAO_EXPEDIDO,)):
        saida.append({"codigo": r["codigo"], "estado": regras.ENTREGUE_NAO_EXPEDIDO,
                      "motivo": "entregue, mas ainda não expedido na Stokki", "desde": r["desde"],
                      "vence_em": r["desde"], "vencido": 1, "detalhe": r["embarcador"] or ""})
    return saida


def placar_anterior(conn: sqlite3.Connection, hoje: date, em_acao: set[str], com_decisao: set[str],
                    no_prazo: set[str]) -> dict | None:
    """Fechamento do último dia útil de rota antes de hoje: programados, entregues,
    não entregues e o que já foi feito com eles. None se não há rota antes."""
    if not (_tabela_existe(conn, "nucleo_rotas") and _tabela_existe(conn, "nucleo_paradas")):
        return None
    # Último DIA ÚTIL com rota: feriado/fim de semana tem 5 paradas de dedicado e
    # mostraria "0 não entregues" (20/11/2026 foi assim).
    candidatos = [r[0] for r in conn.execute("""
        SELECT DISTINCT r.data_rota FROM nucleo_rotas r
        WHERE r.data_rota < ? AND r.cancelada_em IS NULL
          AND EXISTS (SELECT 1 FROM nucleo_paradas p WHERE p.rota_id = r.id)
        ORDER BY r.data_rota DESC LIMIT 15""", (hoje.isoformat(),))]
    dia = next((d for d in candidatos if feriados.eh_dia_util(date.fromisoformat(d))), None)
    if not dia:
        return None
    paradas = conn.execute("""
        SELECT p.codigo, p.situacao FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
        WHERE r.data_rota = ? AND r.cancelada_em IS NULL""", (dia,)).fetchall()
    programados = [p for p in paradas if (p["situacao"] or "") != "CANCELADA"]
    entregues = sum(1 for p in programados if p["situacao"] == "ENTREGUE")
    nao = {banco.normalizar(p["codigo"]) for p in programados if p["situacao"] != "ENTREGUE"}
    sem = nao & em_acao
    com = nao & com_decisao
    prazo = (nao & no_prazo) - sem - com
    return {
        "dia": dia, "programados": len(programados), "entregues": entregues, "nao_entregues": len(nao),
        "sem_decisao": len(sem), "com_decisao": len(com), "no_prazo": len(prazo),
        "resolvidos": len(nao - sem - com - prazo),
    }


def _linha_acao(l: dict, agora: datetime, decisao: dict | None, hoje: date) -> dict:
    acao, dono = regras.acao_e_dono(l["estado"], l.get("motivo"))
    out = {**l, "rotulo": regras.ROTULOS.get(l["estado"], l["estado"]), "acao": acao, "dono": dono,
           "idade": _idade(l["desde"], agora), "dias": _dias(l["desde"], agora), "decisao": None,
           "decisao_vencida": False}
    if decisao and decisao["estado"] == l["estado"]:
        ate = date.fromisoformat(decisao["ate"])
        out["decisao"] = {**decisao, "rotulo": banco.OPCOES_DECISAO.get(decisao["decisao"], decisao["decisao"]),
                          "ate_br": ate.strftime("%d/%m"), "vencida": ate < hoje}
        out["decisao_vencida"] = ate < hoje
    return out


def lista_acao(equipe: str = "", busca: str = "", db_path: Path | None = None,
               agora: datetime | None = None) -> dict:
    """Tudo que precisa de ação, agrupado pelo QUE FAZER, mais o que já tem
    decisão e o placar do último dia de rota. Só lê (vigia_pedidos,
    vigia_decisoes, batimento_pedidos, nucleo_*)."""
    agora = agora or datetime.now()
    hoje = agora.date()
    conn = banco.conectar(db_path)
    try:
        linhas = [dict(r) for r in conn.execute("SELECT * FROM vigia_pedidos")]
        decisoes = {r["codigo"]: dict(r) for r in conn.execute("SELECT * FROM vigia_decisoes")}
        extras = _entregues_nao_expedidos(conn)
        ultima_rodada = max((l["visto_em"] for l in linhas), default=None)
        ultima_listagem = banco.ultima_listagem_completa(conn)

        todas = [_linha_acao(l, agora, decisoes.get(l["codigo"]), hoje) for l in linhas + extras]
        vencidas = [l for l in todas if l["vencido"]]
        # Decisão que ainda vale (mesmo estado, data de retorno de hoje em diante) tira da fila.
        com_decisao = [l for l in vencidas if l["decisao"] and not l["decisao_vencida"]]
        em_acao = [l for l in vencidas if l not in com_decisao]
        no_prazo = {l["codigo"] for l in todas if not l["vencido"]}
        placar = placar_anterior(conn, hoje, {l["codigo"] for l in em_acao},
                                 {l["codigo"] for l in com_decisao}, no_prazo)
    finally:
        conn.close()

    por_equipe = {e: {"equipe": e, "total": 0, "vencida": 0} for e in EQUIPES}
    for l in em_acao:
        por_equipe[l["dono"]]["total"] += 1
        por_equipe[l["dono"]]["vencida"] += 1 if l["decisao_vencida"] else 0

    termo = busca.strip().upper()

    def passa(l: dict) -> bool:
        return ((not equipe or l["dono"] == equipe)
                and (not termo or termo in l["codigo"] or termo in (l.get("detalhe") or "").upper()
                     or termo in (l.get("motivo") or "").upper()))

    grupos: dict[tuple, dict] = {}
    for l in filter(passa, em_acao):
        g = grupos.setdefault((l["dono"], l["acao"], l["estado"]), {
            "dono": l["dono"], "acao": l["acao"], "estado": l["estado"], "rotulo": l["rotulo"],
            "critico": l["estado"] in regras.CRITICOS, "linhas": []})
        g["linhas"].append(l)
    ordem_dono = {e: i for i, e in enumerate(EQUIPES)}
    lista = sorted(grupos.values(), key=lambda g: (ordem_dono[g["dono"]], -len(g["linhas"]), g["acao"]))
    for g in lista:
        g["linhas"].sort(key=lambda l: (not l["decisao_vencida"], l["desde"]))
        g["total"] = len(g["linhas"])
        g["mais_antigo"] = max(g["linhas"], key=lambda l: l["dias"])["idade"]
    decididos = sorted(filter(passa, com_decisao), key=lambda l: l["decisao"]["ate"])
    return {
        "grupos": lista, "com_decisao": decididos, "equipes": list(por_equipe.values()),
        "total_acao": len(em_acao), "decisoes_vencidas": sum(1 for l in em_acao if l["decisao_vencida"]),
        "placar": placar, "ultima_rodada": ultima_rodada, "ultima_listagem_stokki": ultima_listagem,
    }


def data_padrao_retorno(hoje: date | None = None) -> date:
    """Data de retorno sugerida no "Tratar": o próximo dia útil."""
    return feriados.proximo_dia_util(hoje or date.today())


def decidir(codigo: str, estado: str, decisao: str, ate: str, observacao: str, por: str,
            db_path: Path | None = None, hoje: date | None = None) -> None:
    """ValueError com a mensagem pra tela quando algo não vale."""
    try:
        ate_dt = date.fromisoformat(str(ate or ""))
    except ValueError:
        raise ValueError("Informe a data de retorno.") from None
    conn = banco.conectar(db_path)
    try:
        banco.registrar_decisao(conn, codigo, estado, decisao, ate_dt, observacao, por, hoje=hoje)
    finally:
        conn.close()


def desfazer(codigo: str, db_path: Path | None = None) -> bool:
    conn = banco.conectar(db_path)
    try:
        return banco.desfazer_decisao(conn, codigo)
    finally:
        conn.close()
