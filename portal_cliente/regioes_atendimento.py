# -*- coding: utf-8 -*-
"""
regioes_atendimento.py

Dados da página pública app.freshhub.com.br/cliente/regioes (Hugo,
07/10/2026: "deixar esse informativo em uma página no nosso site"):
regiões e dias de visita, galpões com dia próprio e o calendário do mês,
tudo lido da configuração real (roteirizacao/regioes_dia_fixo.py e
regras/feriados.py) pra nunca desatualizar. Sem banco, sem Vuupt.
"""
import calendar
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import regioes_dia_fixo as rdf  # noqa: E402
from regras import feriados  # noqa: E402
from gerar_informativo_regioes import nome_bonito  # noqa: E402  (acento nos nomes das cidades, mesma regra do PDF)

DIAS_CURTOS = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]
DIAS_LONGOS = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
MESES = ["", "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
         "Setembro", "Outubro", "Novembro", "Dezembro"]
# Quantos meses pra trás e pra frente a página deixa navegar.
MESES_ATRAS, MESES_FRENTE = 1, 3
URL_PUBLICA = "https://app.freshhub.com.br/cliente/regioes"
URL_PORTAL = "https://app.freshhub.com.br/cliente"
# Lembrete de virada de mês no painel (/inicio): aparece nos últimos dias.
DIAS_AVISO_VIRADA = 7

GRANDE_SP = {"nome": "Grande São Paulo", "dias": [0, 1, 2, 3, 4], "frequencia": rdf.FREQUENCIA_SEMANAL}


def _prazo_texto(nivel: str) -> str:
    dias, uteis = rdf.PRAZO_POR_NIVEL[nivel]
    return f"até {dias} dias {'úteis' if uteis else 'corridos'}"


def _nivel(regiao: dict) -> str:
    if not regiao.get("externa", False):
        return rdf.NIVEL_INTERNA
    return rdf.NIVEL_QUINZENAL if regiao.get("frequencia") == rdf.FREQUENCIA_QUINZENAL else rdf.NIVEL_SEMANAL


def _frequencia_texto(regiao: dict) -> str:
    if regiao.get("frequencia") == rdf.FREQUENCIA_QUINZENAL:
        return "Quinzenal"
    return "Diária" if len(regiao["dias"]) >= 5 else ("Semanal" if len(regiao["dias"]) == 1 else "2x por semana")


def _dias_texto(dias: list[int]) -> str:
    if len(dias) >= 5:
        return "Segunda a sexta"
    nomes = [DIAS_LONGOS[d] for d in sorted(dias)]
    return (" e ".join(nomes)).capitalize()


def _classe(regiao: dict) -> str:
    if regiao is GRANDE_SP:
        return "diaria"
    if regiao.get("frequencia") == rdf.FREQUENCIA_QUINZENAL:
        return "quinzenal"
    return "externa" if regiao.get("externa", False) else "interna"


def _datas_no_mes(regra: dict, ano: int, mes: int) -> list[date]:
    return [d for d in _dias_do_mes(ano, mes) if rdf.data_valida_na_regiao(regra, d)]


def _dias_do_mes(ano: int, mes: int):
    for dia in range(1, calendar.monthrange(ano, mes)[1] + 1):
        yield date(ano, mes, dia)


def _mes_relativo(ano: int, mes: int, delta: int) -> str:
    total = ano * 12 + (mes - 1) + delta
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def validar_mes(texto: str | None, hoje: date) -> tuple[int, int]:
    """`AAAA-MM` dentro da janela permitida; qualquer outra coisa cai no mês
    corrente (entrada vem da URL)."""
    try:
        ano, mes = (int(p) for p in str(texto or "").split("-", 1))
        date(ano, mes, 1)
    except (TypeError, ValueError):
        return hoje.year, hoje.month
    minimo = _mes_relativo(hoje.year, hoje.month, -MESES_ATRAS)
    maximo = _mes_relativo(hoje.year, hoje.month, MESES_FRENTE)
    if not (minimo <= f"{ano:04d}-{mes:02d}" <= maximo):
        return hoje.year, hoje.month
    return ano, mes


def mes_seguinte(ano: int, mes: int) -> tuple[int, int]:
    return (ano + 1, 1) if mes == 12 else (ano, mes + 1)


def url_do_mes(ano: int, mes: int) -> str:
    return f"{URL_PUBLICA}?mes={ano:04d}-{mes:02d}"


def feriados_uteis_do_mes(ano: int, mes: int) -> list[dict]:
    """Feriados que caem de segunda a sexta (os de fim de semana não mudam nada)."""
    return [{"data": d, "nome": feriados.nome_feriado(d), "semana": DIAS_LONGOS[d.weekday()]}
            for d in _dias_do_mes(ano, mes) if d.weekday() < 5 and feriados.eh_feriado(d)]


def virada_de_mes(hoje: date | None = None, dias_antes: int = DIAS_AVISO_VIRADA) -> dict | None:
    """Resumo do mês SEGUINTE pra equipe conferir antes da virada (Hugo,
    07/10/2026): None fora da janela dos últimos `dias_antes` dias do mês."""
    hoje = hoje or date.today()
    ultimo_dia = calendar.monthrange(hoje.year, hoje.month)[1]
    faltam = ultimo_dia - hoje.day + 1
    if faltam > dias_antes:
        return None
    ano, mes = mes_seguinte(hoje.year, hoje.month)
    dados = montar(ano, mes, hoje)
    return {
        "mes": f"{ano:04d}-{mes:02d}", "titulo_mes": dados["titulo_mes"], "dias_para_virar": faltam,
        "feriados": [{"data": f["data"].isoformat(), "data_br": f["data"].strftime("%d/%m"),
                      "nome": f["nome"], "semana": f["semana"]} for f in feriados_uteis_do_mes(ano, mes)],
        "notas": dados["notas"],
        "quinzenais": [{"nome": r["nome"], "datas": [d.strftime("%d/%m") for d in r["datas"]]}
                       for r in dados["regioes"] if r["quinzenal"]],
        "url": url_do_mes(ano, mes),
    }


def montar(ano: int, mes: int, hoje: date | None = None) -> dict:
    hoje = hoje or date.today()
    regioes = [GRANDE_SP] + list(rdf.REGIOES)
    galpoes = list(rdf.ENDERECOS_DIA_FIXO)

    linhas = []
    for r in regioes:
        linhas.append({
            "nome": "Vale do Paraíba e Alto Tietê" if r["nome"] == "Vale do Paraíba" else r["nome"],
            "cidades": ("São Paulo e cidades até %d km da capital" % int(rdf.RAIO_GRANDE_SP_KM) if r is GRANDE_SP
                        else ", ".join(nome_bonito(c) for c in r["cidades"])),
            "dias": r["dias"], "dias_texto": _dias_texto(r["dias"]),
            "quinzenal": r.get("frequencia") == rdf.FREQUENCIA_QUINZENAL,
            "frequencia": _frequencia_texto(r),
            "prazo": _prazo_texto(rdf.NIVEL_INTERNA if r is GRANDE_SP else _nivel(r)),
            "classe": _classe(r),
            "datas": _datas_no_mes(r, ano, mes),
        })
    galpoes_linhas = [{
        "nome": g["nome"], "endereco": g.get("endereco") or g.get("cidade", "").title(),
        "dias": g["dias"], "dias_texto": _dias_texto(g["dias"]), "classe": "galpao",
        "datas": _datas_no_mes(g, ano, mes),
    } for g in galpoes]

    # Chips por dia: regiões de dia fixo (sem Grande SP, que é todo dia) e galpões.
    com_chip = [(l, r) for l, r in zip(linhas[1:], regioes[1:])] + list(zip(galpoes_linhas, galpoes))
    celulas = {}
    feriados_do_mes = []
    for d in _dias_do_mes(ano, mes):
        cel = {"data": d, "dia": d.day, "semana": DIAS_LONGOS[d.weekday()].capitalize(),
               "fds": d.weekday() >= 5, "feriado": feriados.nome_feriado(d), "hoje": d == hoje, "chips": []}
        if cel["feriado"] and not cel["fds"]:
            feriados_do_mes.append(cel)
        if feriados.eh_dia_util(d):
            for linha, regra in com_chip:
                if rdf.data_valida_na_regiao(regra, d):
                    cel["chips"].append({"nome": linha["nome"].replace(" e Alto Tietê", ""), "classe": linha["classe"],
                                         "transferida": not rdf.visita_nominal(regra, d)})
        celulas[d] = cel

    semanas = [[celulas.get(d) if d.month == mes else None for d in semana]
               for semana in calendar.Calendar(firstweekday=0).monthdatescalendar(ano, mes)]

    notas = []
    for cel in feriados_do_mes:
        d = cel["data"]
        afetadas = [l["nome"].replace(" e Alto Tietê", "") for l, r in com_chip if rdf.visita_nominal(r, d)]
        seguinte = feriados.proximo_dia_util(d)
        texto = f"{d:%d/%m} ({DIAS_LONGOS[d.weekday()]}) é feriado ({cel['feriado']}) e não há visita."
        if afetadas:
            texto += (f" As entregas de {', '.join(afetadas)} previstas para esse dia são feitas no dia útil "
                      f"seguinte, {DIAS_LONGOS[seguinte.weekday()]} {seguinte:%d/%m}.")
        notas.append(texto)

    return {
        "ano": ano, "mes": mes, "titulo_mes": f"{MESES[mes]} de {ano}",
        "mes_anterior": _mes_relativo(ano, mes, -1), "mes_seguinte": _mes_relativo(ano, mes, 1),
        "pode_voltar": _mes_relativo(ano, mes, -1) >= _mes_relativo(hoje.year, hoje.month, -MESES_ATRAS),
        "pode_avancar": _mes_relativo(ano, mes, 1) <= _mes_relativo(hoje.year, hoje.month, MESES_FRENTE),
        "regioes": linhas, "galpoes": galpoes_linhas, "semanas": semanas, "notas": notas,
        "dias_curtos": DIAS_CURTOS, "hoje": hoje,
    }
