# -*- coding: utf-8 -*-
"""
Relatorio de pagamento dos motoristas ESPORADICOS (/financeiro/esporadicos).

O financeiro escolhe o periodo (data da rota) e recebe, por motorista, as
rotas com km e valor (tarifa ou valor combinado + motivo), o pedagio/despesa
aprovado e o pendente, e os dados de pagamento (CPF e PIX). Os valores vem
de nucleo/financeiro.extrato_motorista: o mesmo calculo do extrato que o
motorista ve no app, entao o relatorio nunca diverge dele.

Quem entra: bd_motoristas.POSICAO = ESPORADICO e ATIVO. Mesma pessoa em mais
de uma linha (dois veiculos/agent_id) vira um motorista so, pelo CPF.
Banco/agencia nao sao cadastrados em lugar nenhum: so a chave PIX.
"""
import csv
import io
import re
import sqlite3
from datetime import date

from nucleo import banco, financeiro


def _digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _ativo(valor) -> bool:
    return str(valor or "").strip().upper() in ("1", "TRUE", "SIM", "S", "VERDADEIRO")


def _esporadicos(conn) -> list[dict]:
    """Uma entrada por pessoa (CPF; sem CPF, a propria linha), so com agent_id."""
    try:
        pix = {_digitos(r["cpf"]): (r["chave_pix"] or "").strip() for r in
               conn.execute("SELECT cpf, chave_pix FROM motoristas WHERE chave_pix IS NOT NULL AND chave_pix != ''")}
    except Exception:       # tabela do app ainda nao existe neste banco
        pix = {}
    pessoas: dict[str, dict] = {}
    for r in conn.execute("SELECT * FROM bd_motoristas ORDER BY id"):
        if (r["POSICAO"] or "").strip().upper() != "ESPORADICO" or not _ativo(r["ATIVO"]):
            continue
        if r["AGENT_ID_VUUPT"] is None or not (r["NOME_MOTORISTA"] or "").strip():
            continue
        cpf = _digitos(r["CPF_MOTORISTA"])
        chave = cpf or f"id:{r['id']}"
        p = pessoas.setdefault(chave, {
            "chave": chave, "nome": r["NOME_MOTORISTA"].strip(), "cpf": cpf, "pix": pix.get(cpf, ""), "agentes": {}})
        p["agentes"][int(r["AGENT_ID_VUUPT"])] = (r["TIPO_VEICULO"] or "").strip() or None
    return sorted(pessoas.values(), key=lambda p: p["nome"].casefold())


def _mes(ano: int, mes: int, delta: int) -> tuple[int, int]:
    t = ano * 12 + (mes - 1) + delta
    return t // 12, t % 12 + 1


def ciclo_atual(hoje: date) -> tuple[date, date]:
    """Ciclos fixos de pagamento: 10 a 24 do mes, e 25 ao dia 9 do mes seguinte."""
    if 10 <= hoje.day <= 24:
        return date(hoje.year, hoje.month, 10), date(hoje.year, hoje.month, 24)
    ano, mes = (hoje.year, hoje.month) if hoje.day >= 25 else _mes(hoje.year, hoje.month, -1)
    ano2, mes2 = _mes(ano, mes, 1)
    return date(ano, mes, 25), date(ano2, mes2, 9)


def ciclos_recentes(hoje: date, n: int = 10) -> list[dict]:
    """Do ciclo de hoje pra tras: [{rotulo, de, ate}] com datas ISO."""
    de, ate = ciclo_atual(hoje)
    lista = []
    for _ in range(n):
        lista.append({"rotulo": f"{de.day:02d}/{de.month:02d} a {ate.day:02d}/{ate.month:02d}/{ate.year}",
                      "de": de.isoformat(), "ate": ate.isoformat()})
        if de.day == 10:         # anterior: 25 do mes passado ao dia 9 deste
            ano, mes = _mes(de.year, de.month, -1)
            nde, nate = date(ano, mes, 25), date(de.year, de.month, 9)
        else:                    # anterior: 10 a 24 do mes em que este ciclo comecou
            nde, nate = date(de.year, de.month, 10), date(de.year, de.month, 24)
        de, ate = nde, nate
    return lista


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pagamentos_esporadicos (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chave        TEXT NOT NULL,          -- CPF (so digitos) ou id:<bd_motoristas.id> de quem nao tem CPF
    nome         TEXT,
    cpf          TEXT,
    valor        REAL NOT NULL,          -- soma (rotas + pedagio aprovado) das rotas pagas no ato
    periodo_ini  TEXT,                   -- periodo da tela quando o financeiro marcou (so informativo)
    periodo_fim  TEXT,
    observacao   TEXT,                   -- ex.: id da transferencia/comprovante
    pago_em      TEXT NOT NULL,
    pago_por     TEXT,
    desfeito_em  TEXT,
    desfeito_por TEXT
);
CREATE TABLE IF NOT EXISTS pagamentos_esporadicos_rotas (
    pagamento_id     INTEGER NOT NULL REFERENCES pagamentos_esporadicos(id),
    rota_id          INTEGER NOT NULL UNIQUE,    -- uma rota so e paga uma vez
    valor_rota       REAL,
    pedagio_aprovado REAL
);
"""

FILTROS = ("pendentes", "pagos", "todos")


class ErroPagamento(ValueError):
    """Mensagem pronta pra tela (o JSON devolve em `erro`)."""


def _garantir_tabelas(conn) -> None:
    conn.executescript(_SCHEMA)


def _valor_linha(l: dict) -> float:
    """O que a rota vale no pagamento: rota + pedagio aprovado. Sem valor definido nao se paga."""
    return 0.0 if l["valor"] is None else round(l["valor"] + l["pedagio_aprovado"], 2)


def gerar(data_ini: str, data_fim: str, filtro: str = "pendentes", conn=None) -> dict:
    """Motoristas esporadicos com rota no periodo. `filtro`: pendentes | pagos | todos
    (decide quais ROTAS aparecem; motorista sem rota visivel some)."""
    if filtro not in FILTROS:
        filtro = "pendentes"
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        _garantir_tabelas(conn)
        pagas = {r["rota_id"]: {"pagamento_id": r["pagamento_id"], "pago_em": r["pago_em"], "pago_por": r["pago_por"]}
                 for r in conn.execute(
                     "SELECT pr.rota_id, p.id AS pagamento_id, p.pago_em, p.pago_por FROM pagamentos_esporadicos_rotas pr "
                     "JOIN pagamentos_esporadicos p ON p.id = pr.pagamento_id WHERE p.desfeito_em IS NULL")}
        motoristas = []
        for p in _esporadicos(conn):
            todas = []
            for agent_id, tipo in p["agentes"].items():
                todas += financeiro.extrato_motorista(agent_id, data_ini, data_fim, tipo, conn=conn)["linhas"]
            for l in todas:
                l["pago"] = pagas.get(l["rota_id"])
            linhas = [l for l in todas if filtro == "todos" or (l["pago"] is None) == (filtro == "pendentes")]
            if not linhas:
                continue
            linhas.sort(key=lambda l: (l["data_rota"] or "", l["rota_id"] or 0))
            total_pago = round(sum(_valor_linha(l) for l in linhas if l["pago"]), 2)
            total_pendente = round(sum(_valor_linha(l) for l in linhas if not l["pago"]), 2)
            ids_pag = sorted({l["pago"]["pagamento_id"] for l in linhas if l["pago"]})
            pagamentos = [dict(r) for r in conn.execute(
                f"SELECT id, valor, pago_em, pago_por, observacao FROM pagamentos_esporadicos "
                f"WHERE id IN ({','.join('?' * len(ids_pag))}) ORDER BY id", ids_pag)] if ids_pag else []
            motoristas.append({
                "chave": p["chave"], "nome": p["nome"], "cpf": p["cpf"], "pix": p["pix"],
                "veiculo": ", ".join(sorted({t for t in p["agentes"].values() if t})),
                "linhas": linhas, "rotas": len(linhas), "pagamentos": pagamentos,
                "total_rotas": round(sum(l["valor"] or 0 for l in linhas), 2),
                "pedagio_aprovado": round(sum(l["pedagio_aprovado"] for l in linhas if l["valor"] is not None), 2),
                "pedagio_pendente": round(sum(l["pedagio_pendente"] for l in linhas), 2),
                "total": round(total_pago + total_pendente, 2), "total_pago": total_pago, "total_pendente": total_pendente,
                "rotas_sem_valor": sum(1 for l in linhas if l["valor"] is None),
                "rotas_sem_gps": sum(1 for l in linhas if l["km_fonte"] != "GPS_APP"),
                "pode_pagar": any(l["valor"] is not None and not l["pago"] for l in linhas),
            })
        return {
            "inicio": str(data_ini), "fim": str(data_fim), "filtro": filtro, "motoristas": motoristas,
            "total": round(sum(m["total"] for m in motoristas), 2),
            "total_pago": round(sum(m["total_pago"] for m in motoristas), 2),
            "total_pendente": round(sum(m["total_pendente"] for m in motoristas), 2),
            "pedagio_pendente": round(sum(m["pedagio_pendente"] for m in motoristas), 2),
            "rotas_sem_valor": sum(m["rotas_sem_valor"] for m in motoristas),
            "rotas_sem_gps": sum(m["rotas_sem_gps"] for m in motoristas),
        }
    finally:
        if fechar:
            conn.close()


def marcar_pago(chave: str, data_ini: str, data_fim: str, usuario: str, observacao: str = "", conn=None) -> dict:
    """Paga TUDO o que esta pendente (e tem valor) do motorista no periodo. O valor sai do banco, nunca
    do navegador. Rota ja paga nao entra (UNIQUE em rota_id): clique duplo nao paga duas vezes."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        m = next((x for x in gerar(data_ini, data_fim, "pendentes", conn=conn)["motoristas"] if x["chave"] == chave), None)
        linhas = [l for l in (m["linhas"] if m else []) if l["valor"] is not None]
        if not linhas:
            raise ErroPagamento("Nao ha rota pendente com valor definido para este motorista no periodo.")
        total = round(sum(_valor_linha(l) for l in linhas), 2)
        try:
            cur = conn.execute(
                "INSERT INTO pagamentos_esporadicos (chave, nome, cpf, valor, periodo_ini, periodo_fim, observacao, pago_em, pago_por) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chave, m["nome"], m["cpf"], total, data_ini, data_fim, (observacao or "").strip()[:300], banco.agora(), usuario))
            conn.executemany(
                "INSERT INTO pagamentos_esporadicos_rotas (pagamento_id, rota_id, valor_rota, pedagio_aprovado) VALUES (?, ?, ?, ?)",
                [(cur.lastrowid, l["rota_id"], l["valor"], l["pedagio_aprovado"]) for l in linhas])
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            raise ErroPagamento("Alguma dessas rotas acabou de ser paga por outra pessoa. Recarregue a tela.")
        return {"id": cur.lastrowid, "valor": total, "rotas": len(linhas)}
    finally:
        if fechar:
            conn.close()


def desfazer_pago(pagamento_id: int, usuario: str, conn=None) -> None:
    """Desfaz um pagamento marcado por engano: as rotas voltam a pendentes. O registro fica (desfeito_em/por)."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        _garantir_tabelas(conn)
        cur = conn.execute("UPDATE pagamentos_esporadicos SET desfeito_em = ?, desfeito_por = ? "
                           "WHERE id = ? AND desfeito_em IS NULL", (banco.agora(), usuario, pagamento_id))
        if cur.rowcount != 1:
            raise ErroPagamento("Pagamento nao encontrado ou ja desfeito.")
        conn.execute("DELETE FROM pagamentos_esporadicos_rotas WHERE pagamento_id = ?", (pagamento_id,))
        conn.commit()
    finally:
        if fechar:
            conn.close()


FONTES_KM = {"GPS_APP": "GPS do app", "INFORMADO": "Informado pelo motorista", "ESTIMADO": "Estimado (sem GPS)"}


def fonte_km(l: dict) -> str:
    """De onde veio o km da rota: so 'GPS do app' e medicao; o resto o financeiro confere."""
    return FONTES_KM.get(l["km_fonte"], "Sem km")


_SO_NUMERO = re.compile(r"^[+-]?\d[\d\s().-]*$")


def _txt(v):
    """Celula de texto livre (nome, PIX, motivo...): o Excel executa como formula o que comeca
    com = + - @ (ou tab/CR). Prefixa ' nesses casos. Numero/telefone puro (PIX +5511...) fica."""
    if not isinstance(v, str) or not v or v[0] not in ("=", "+", "-", "@", chr(9), chr(13)) or _SO_NUMERO.match(v):
        return v
    return "'" + v


def _num(v) -> str:
    return "" if v is None else f"{v:.2f}".replace(".", ",")


def _origem(l: dict) -> str:
    if l["valor_combinado"]:
        return "Combinado"
    return "A definir" if l["sem_tarifa"] else "Tarifa"


def _data_br(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else (iso or "")


def _situacao(l: dict) -> str:
    if l["pago"]:
        return f"Pago em {_data_br(l['pago']['pago_em'])}"
    return "A definir" if l["valor"] is None else "Pendente"


def csv_detalhe(rel: dict) -> str:
    """Uma linha por rota. `;` e virgula decimal: abre direto no Excel em pt-BR."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\r\n")
    w.writerow(["Motorista", "CPF", "PIX", "Veiculo", "Data da rota", "Rota", "Entregues", "Insucessos",
                "Km", "Fonte do km", "Origem do valor", "Motivo do valor combinado", "Valor da rota",
                "Pedagio aprovado", "Pedagio pendente", "Pagamento"])
    for m in rel["motoristas"]:
        for l in m["linhas"]:
            w.writerow([_txt(c) if isinstance(c, str) else c for c in (
                m["nome"], m["cpf"], m["pix"], l["tipo_veiculo_motorista"] or "", l["data_rota"],
                l["nome"] or l["rota_id"], l["entregues"], l["insucessos"], _num(l["km"]),
                fonte_km(l), _origem(l), l["valor_combinado_motivo"] or "",
                _num(l["valor"]), _num(l["pedagio_aprovado"]), _num(l["pedagio_pendente"]), _situacao(l))])
    return saida.getvalue()


def csv_resumo(rel: dict) -> str:
    """Uma linha por motorista: o que o financeiro paga."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\r\n")
    w.writerow(["Motorista", "CPF", "PIX", "Veiculo", "Rotas", "Rotas sem GPS", "Rotas sem valor", "Total das rotas",
                "Pedagio aprovado", "Total", "Pago", "Pendente", "Pedagio pendente (fora do total)"])
    for m in rel["motoristas"]:
        w.writerow([_txt(m["nome"]), m["cpf"], _txt(m["pix"]), _txt(m["veiculo"]), m["rotas"], m["rotas_sem_gps"], m["rotas_sem_valor"],
                    _num(m["total_rotas"]), _num(m["pedagio_aprovado"]), _num(m["total"]),
                    _num(m["total_pago"]), _num(m["total_pendente"]), _num(m["pedagio_pendente"])])
    w.writerow(["TOTAL", "", "", "", sum(m["rotas"] for m in rel["motoristas"]), rel["rotas_sem_gps"], rel["rotas_sem_valor"], "", "",
                _num(rel["total"]), _num(rel["total_pago"]), _num(rel["total_pendente"]),
                _num(rel["pedagio_pendente"])])
    return saida.getvalue()
