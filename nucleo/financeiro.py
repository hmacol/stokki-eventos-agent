# -*- coding: utf-8 -*-
"""
nucleo/financeiro.py

Extrato do motorista sobre nucleo_rotas + regras/tarifa_motorista.py --
Fase A (o app consome isso na Fase B via GET /financeiro).

Km cobrado por rota: `km_real` quando existir (GPS do app, Fase B),
senão `km_estimado` do rascunho (Google Directions, ver
rascunhos_rota.recalcular_km). A fonte usada vai junto em cada linha
(`km_fonte`) -- o motorista e o Hugo sempre sabem se o valor é
provisório.

Tarifa é do veículo DO MOTORISTA (`tipo_veiculo_motorista`, coluna
TIPO_VEICULO da planilha / motoristas.tipo_veiculo), não da
classificação da rota -- quem chama resolve isso e passa (o painel usa
CatalogoMotoristas, o app usa a tabela motoristas). Rota cancelada não
entra. Tipo sem tarifa (VUC/3-4/Truck): linha entra com valor None e
`sem_tarifa=True`, o total ignora e o extrato avisa "a definir".
"""
import sqlite3
from collections import defaultdict
from datetime import date

from nucleo import banco, rotas as nucleo_rotas
from regras import tarifa_motorista


def _km_da_rota(rota: dict) -> tuple[float | None, str | None]:
    if rota.get("km_real") is not None:
        return float(rota["km_real"]), rota.get("km_fonte") or "GPS_APP"
    if rota.get("km_estimado") is not None:
        return float(rota["km_estimado"]), "ESTIMADO"
    return None, None


def linha_extrato(rota: dict, tipo_veiculo_motorista: str | None,
                  tarifas: dict[str, tarifa_motorista.Tarifa] | None = None) -> dict:
    km, fonte = _km_da_rota(rota)
    resultado = tarifa_motorista.calcular_valor_rota(tipo_veiculo_motorista, km, tarifas)
    linha = {
        "rota_id": rota.get("id"), "data_rota": rota.get("data_rota"), "nome": rota.get("nome"),
        "provedor": rota.get("provedor"), "status": rota.get("status"),
        "total_paradas": rota.get("total_paradas"), "entregues": rota.get("entregues"),
        "insucessos": rota.get("insucessos"),
        "km": km, "km_fonte": fonte, "km_provisorio": fonte != "GPS_APP",
        "tipo_veiculo_motorista": tipo_veiculo_motorista,
        "sem_tarifa": resultado is None,
        "valor": None, "detalhe_tarifa": None,
    }
    if resultado is not None:
        linha["valor"] = resultado.valor_total
        linha["detalhe_tarifa"] = resultado.como_dict()
    return linha


def extrato_motorista(agent_id: int, data_ini: date | str, data_fim: date | str,
                      tipo_veiculo_motorista: str | None,
                      conn: sqlite3.Connection | None = None) -> dict:
    """Linhas por rota + totais por dia + total do período."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        tarifas = tarifa_motorista.carregar_tarifas(conn)
        rotas = [r for r in nucleo_rotas.listar_rotas_motorista(agent_id, data_ini, data_fim, conn=conn)
                 if r.get("status") != banco.ROTA_CANCELADA]
    finally:
        if fechar:
            conn.close()

    linhas = [linha_extrato(r, tipo_veiculo_motorista, tarifas) for r in rotas]
    por_dia: dict[str, dict] = defaultdict(lambda: {"rotas": 0, "valor": 0.0, "km": 0.0, "sem_tarifa": 0, "provisorio": False})
    total = 0.0
    sem_tarifa = 0
    provisorio = False
    for l in linhas:
        d = por_dia[l["data_rota"]]
        d["rotas"] += 1
        d["km"] += l["km"] or 0.0
        if l["valor"] is None:
            d["sem_tarifa"] += 1
            sem_tarifa += 1
        else:
            d["valor"] = round(d["valor"] + l["valor"], 2)
            total = round(total + l["valor"], 2)
        if l["km_provisorio"]:
            d["provisorio"] = True
            provisorio = True

    return {
        "agent_id": agent_id,
        "periodo": {"inicio": str(data_ini), "fim": str(data_fim)},
        "tipo_veiculo_motorista": tipo_veiculo_motorista,
        "linhas": linhas,
        "por_dia": [{"data": k, **v} for k, v in sorted(por_dia.items())],
        "total": total,
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