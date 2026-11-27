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
        p = pessoas.setdefault(cpf or f"id:{r['id']}", {
            "nome": r["NOME_MOTORISTA"].strip(), "cpf": cpf, "pix": pix.get(cpf, ""), "agentes": {}})
        p["agentes"][int(r["AGENT_ID_VUUPT"])] = (r["TIPO_VEICULO"] or "").strip() or None
    return sorted(pessoas.values(), key=lambda p: p["nome"].casefold())


def gerar(data_ini: str, data_fim: str, conn=None) -> dict:
    """Motoristas esporadicos que tiveram rota no periodo, com o extrato de cada um."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        motoristas = []
        for p in _esporadicos(conn):
            linhas, total_rotas, ped_aprov, ped_pend, sem_tarifa = [], 0.0, 0.0, 0.0, 0
            for agent_id, tipo in p["agentes"].items():
                ex = financeiro.extrato_motorista(agent_id, data_ini, data_fim, tipo, conn=conn)
                linhas += ex["linhas"]
                total_rotas += ex["total_rotas"]
                ped_aprov += ex["total_pedagio"]
                ped_pend += ex["pedagio_pendente"]
                sem_tarifa += ex["rotas_sem_tarifa"]
            if not linhas:
                continue
            sem_gps = sum(1 for l in linhas if l["km_fonte"] != "GPS_APP")
            linhas.sort(key=lambda l: (l["data_rota"] or "", l["rota_id"] or 0))
            motoristas.append({
                "nome": p["nome"], "cpf": p["cpf"], "pix": p["pix"],
                "veiculo": ", ".join(sorted({t for t in p["agentes"].values() if t})),
                "linhas": linhas, "rotas": len(linhas),
                "total_rotas": round(total_rotas, 2), "pedagio_aprovado": round(ped_aprov, 2),
                "pedagio_pendente": round(ped_pend, 2), "total": round(total_rotas + ped_aprov, 2),
                "rotas_sem_valor": sem_tarifa, "rotas_sem_gps": sem_gps,
            })
        return {
            "inicio": str(data_ini), "fim": str(data_fim), "motoristas": motoristas,
            "total": round(sum(m["total"] for m in motoristas), 2),
            "pedagio_pendente": round(sum(m["pedagio_pendente"] for m in motoristas), 2),
            "rotas_sem_valor": sum(m["rotas_sem_valor"] for m in motoristas),
            "rotas_sem_gps": sum(m["rotas_sem_gps"] for m in motoristas),
        }
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


def csv_detalhe(rel: dict) -> str:
    """Uma linha por rota. `;` e virgula decimal: abre direto no Excel em pt-BR."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\r\n")
    w.writerow(["Motorista", "CPF", "PIX", "Veiculo", "Data da rota", "Rota", "Entregues", "Insucessos",
                "Km", "Fonte do km", "Origem do valor", "Motivo do valor combinado", "Valor da rota",
                "Pedagio aprovado", "Pedagio pendente"])
    for m in rel["motoristas"]:
        for l in m["linhas"]:
            w.writerow([_txt(c) if isinstance(c, str) else c for c in (
                m["nome"], m["cpf"], m["pix"], l["tipo_veiculo_motorista"] or "", l["data_rota"],
                l["nome"] or l["rota_id"], l["entregues"], l["insucessos"], _num(l["km"]),
                fonte_km(l), _origem(l), l["valor_combinado_motivo"] or "",
                _num(l["valor"]), _num(l["pedagio_aprovado"]), _num(l["pedagio_pendente"]))])
    return saida.getvalue()


def csv_resumo(rel: dict) -> str:
    """Uma linha por motorista: o que o financeiro paga."""
    saida = io.StringIO()
    w = csv.writer(saida, delimiter=";", lineterminator="\r\n")
    w.writerow(["Motorista", "CPF", "PIX", "Veiculo", "Rotas", "Rotas sem GPS", "Rotas sem valor", "Total das rotas",
                "Pedagio aprovado", "Total a pagar", "Pedagio pendente (fora do total)"])
    for m in rel["motoristas"]:
        w.writerow([_txt(m["nome"]), m["cpf"], _txt(m["pix"]), _txt(m["veiculo"]), m["rotas"], m["rotas_sem_gps"], m["rotas_sem_valor"],
                    _num(m["total_rotas"]), _num(m["pedagio_aprovado"]), _num(m["total"]),
                    _num(m["pedagio_pendente"])])
    w.writerow(["TOTAL", "", "", "", sum(m["rotas"] for m in rel["motoristas"]), rel["rotas_sem_gps"], rel["rotas_sem_valor"], "", "",
                _num(rel["total"]), _num(rel["pedagio_pendente"])])
    return saida.getvalue()
