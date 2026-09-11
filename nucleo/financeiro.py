# -*- coding: utf-8 -*-
"""
nucleo/financeiro.py

Extrato do motorista sobre nucleo_rotas + regras/tarifa_motorista.py --
Fase A (o app consome isso na Fase B via GET /financeiro).

Km cobrado por rota: regras/km_cobrado.py (decisão do Hugo, 11/09):
`km_real` do GPS quando existir, senão `km_estimado` do rascunho; a volta
ao CD só conta com insucesso/parcial ou parada fora da Grande SP. A fonte
e o motivo vão em cada linha (`km_fonte`, `km_detalhe`) -- o motorista e
o Hugo sempre sabem se o valor é provisório.

Pedágio (Hugo, 11/09): reembolsado À PARTE. Só o que foi APROVADO no
painel entra no total (`pedagio_aprovado`); o informado e ainda pendente
aparece separado (`pedagio_pendente`) pra o motorista acompanhar.

Tarifa é do veículo DO MOTORISTA (`tipo_veiculo_motorista`, coluna
TIPO_VEICULO da planilha / motoristas.tipo_veiculo), não da
classificação da rota -- quem chama resolve isso e passa (o painel usa
CatalogoMotoristas, o app usa a tabela motoristas). Rota cancelada não
entra. Tipo sem tarifa (3/4, Truck): linha entra com valor None e
`sem_tarifa=True`, o total ignora e o extrato avisa "a definir".
Insucesso NÃO desconta (rota paga integral); reentrega (-R) é parada
normal da rota em que foi roteirizada.
"""
import sqlite3
from collections import defaultdict
from datetime import date

from nucleo import banco, rotas as nucleo_rotas
from regras import km_cobrado, tarifa_motorista


def _paradas_para_km(conn: sqlite3.Connection, rota_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT situacao, latitude, longitude FROM nucleo_paradas WHERE rota_id = ? AND situacao != ? ORDER BY ordem",
        (rota_id, banco.PARADA_CANCELADA),
    ).fetchall()]


def _pedagios_da_rota(conn: sqlite3.Connection, rota_id: int) -> dict:
    aprovado = pendente = rejeitado = 0.0
    n = 0
    for r in conn.execute("SELECT status, valor_informado, valor_aprovado FROM nucleo_pedagios WHERE rota_id = ?", (rota_id,)):
        n += 1
        if r["status"] == banco.PEDAGIO_APROVADO:
            aprovado += float(r["valor_aprovado"] if r["valor_aprovado"] is not None else r["valor_informado"])
        elif r["status"] == banco.PEDAGIO_REJEITADO:
            rejeitado += float(r["valor_informado"])
        else:
            pendente += float(r["valor_informado"])
    return {"pedagio_aprovado": round(aprovado, 2), "pedagio_pendente": round(pendente, 2),
            "pedagio_rejeitado": round(rejeitado, 2), "pedagios": n}


def linha_extrato(rota: dict, tipo_veiculo_motorista: str | None,
                  tarifas: dict[str, tarifa_motorista.Tarifa] | None = None,
                  paradas: list[dict] | None = None, pedagios: dict | None = None) -> dict:
    kmc = km_cobrado.calcular_km_cobrado(rota, paradas or [])
    resultado = tarifa_motorista.calcular_valor_rota(tipo_veiculo_motorista, kmc.km, tarifas)
    ped = pedagios or {"pedagio_aprovado": 0.0, "pedagio_pendente": 0.0, "pedagio_rejeitado": 0.0, "pedagios": 0}
    linha = {
        "rota_id": rota.get("id"), "data_rota": rota.get("data_rota"), "nome": rota.get("nome"),
        "provedor": rota.get("provedor"), "status": rota.get("status"),
        "total_paradas": rota.get("total_paradas"), "entregues": rota.get("entregues"),
        "insucessos": rota.get("insucessos"),
        "km": kmc.km, "km_fonte": kmc.fonte, "km_provisorio": kmc.provisorio,
        "km_detalhe": kmc.como_dict(),
        "tipo_veiculo_motorista": tipo_veiculo_motorista,
        "sem_tarifa": resultado is None,
        "valor": None, "detalhe_tarifa": None,
        **ped,
        "valor_com_pedagio": None,
    }
    if resultado is not None:
        linha["valor"] = resultado.valor_total
        linha["detalhe_tarifa"] = resultado.como_dict()
        linha["valor_com_pedagio"] = round(resultado.valor_total + ped["pedagio_aprovado"], 2)
    return linha


def extrato_motorista(agent_id: int, data_ini: date | str, data_fim: date | str,
                      tipo_veiculo_motorista: str | None,
                      conn: sqlite3.Connection | None = None) -> dict:
    """Linhas por rota + totais por dia + total do período (rotas +
    pedágio aprovado)."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        tarifas = tarifa_motorista.carregar_tarifas(conn)
        rotas = [r for r in nucleo_rotas.listar_rotas_motorista(agent_id, data_ini, data_fim, conn=conn)
                 if r.get("status") != banco.ROTA_CANCELADA]
        linhas = [
            linha_extrato(r, tipo_veiculo_motorista, tarifas, _paradas_para_km(conn, r["id"]), _pedagios_da_rota(conn, r["id"]))
            for r in rotas
        ]
    finally:
        if fechar:
            conn.close()

    por_dia: dict[str, dict] = defaultdict(lambda: {
        "rotas": 0, "valor": 0.0, "km": 0.0, "sem_tarifa": 0, "provisorio": False,
        "pedagio_aprovado": 0.0, "pedagio_pendente": 0.0,
    })
    total_rotas = 0.0
    total_pedagio = 0.0
    pedagio_pendente = 0.0
    sem_tarifa = 0
    provisorio = False
    for l in linhas:
        d = por_dia[l["data_rota"]]
        d["rotas"] += 1
        d["km"] += l["km"] or 0.0
        d["pedagio_aprovado"] = round(d["pedagio_aprovado"] + l["pedagio_aprovado"], 2)
        d["pedagio_pendente"] = round(d["pedagio_pendente"] + l["pedagio_pendente"], 2)
        total_pedagio = round(total_pedagio + l["pedagio_aprovado"], 2)
        pedagio_pendente = round(pedagio_pendente + l["pedagio_pendente"], 2)
        if l["valor"] is None:
            d["sem_tarifa"] += 1
            sem_tarifa += 1
        else:
            d["valor"] = round(d["valor"] + l["valor"], 2)
            total_rotas = round(total_rotas + l["valor"], 2)
        if l["km_provisorio"]:
            d["provisorio"] = True
            provisorio = True
    for d in por_dia.values():
        # valor do dia = rotas + pedágio aprovado (o que o motorista recebe)
        d["valor"] = round(d["valor"] + d["pedagio_aprovado"], 2)

    return {
        "agent_id": agent_id,
        "periodo": {"inicio": str(data_ini), "fim": str(data_fim)},
        "tipo_veiculo_motorista": tipo_veiculo_motorista,
        "linhas": linhas,
        "por_dia": [{"data": k, **v} for k, v in sorted(por_dia.items())],
        "total": round(total_rotas + total_pedagio, 2),
        "total_rotas": total_rotas,
        "total_pedagio": total_pedagio,
        "pedagio_pendente": pedagio_pendente,
        "rotas_sem_tarifa": sem_tarifa,
        "valores_provisorios": provisorio,
    }


def fechamento_periodo(data_ini: date | str, data_fim: date | str,
                       tipo_por_agent: dict[int, str | None],
                       conn: sqlite3.Connection | None = None) -> list[dict]:
    """Um extrato por motorista que teve rota no período (visão do
    Hugo/painel). `tipo_por_agent` vem do CatalogoMotoristas."""
    ini = data_ini.isoformat() if isinstance(data_ini, date) else str(data_ini)
    fim = data_fim.isoformat() if isinstance(data_fim, date) else str(data_fim)
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        agentes = [r[0] for r in conn.execute("""
            SELECT DISTINCT agent_id FROM nucleo_rotas
            WHERE agent_id IS NOT NULL AND data_rota BETWEEN ? AND ? AND status != ?
            ORDER BY agent_id
        """, (ini, fim, banco.ROTA_CANCELADA)).fetchall()]
        return [extrato_motorista(a, ini, fim, tipo_por_agent.get(a), conn=conn) for a in agentes]
    finally:
        if fechar:
            conn.close()
