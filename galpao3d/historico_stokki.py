# -*- coding: utf-8 -*-
"""
galpao3d/historico_stokki.py

Le a pagina /outbound/show/{id} da Stokki (HTML) e extrai a linha do tempo
("Historico do pedido"), o operador da separacao, a conferencia e os itens.
Funcoes puras, sem rede. Horarios ja vem em Sao Paulo.
"""
import re
from datetime import datetime

from stokki.cancelar import _limpar

# Confirmado na sonda da Task 1 (API -> texto da situacao na tela).
# "Em espera" nao aparece nas fixtures; grafia conforme combinado (e minusculo).
ESTADO_API_PARA_SITUACAO = {
    "Open": "Aberto",
    "Separating": "Em separação",
    "Ready to Pack": "Em Conferência",
    "On hold": "Em espera",
    "Waiting for Carrier": "Aguardando Transportador",
    "Sent": "Enviado",
    "Canceled": "Cancelado",
}

# Eventos da linha do tempo que nao sao mudanca de situacao.
_NAO_SITUACAO = {"Data do Pedido", "Importação"}

_NAO_INFORMADO = "Não informado"

_RE_BLOCO_DATA = re.compile(r'class="bg-red">\s*(\d{2}/\d{2}/\d{4})\s*<')
_RE_ITEM = re.compile(
    r'<span class="time"><i class="fas fa-clock"></i>\s*(\d{2}:\d{2})\s*</span>'
    r'(?:\s*<span class="time"><i class="fas fa-user"></i>\s*([^<]*?)\s*</span>)?'
    r'\s*<h3 class="timeline-header">\s*([^<]*?)\s*</h3>',
    re.S,
)
_RE_DATA_HORA = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")


def _linha_do_tempo(html: str) -> list:
    """Eventos de situacao em ordem crescente. A data do bloco vale para as
    horas abaixo dela. "Data do Pedido" e "Importação" nao sao mudanca de
    situacao e ficam de fora; evento sem usuario e mantido com usuario ""."""
    i = html.rfind("Histórico do pedido")
    if i < 0:
        return []
    trecho = html[i:]
    tokens = sorted(
        [(m.start(), "d", m) for m in _RE_BLOCO_DATA.finditer(trecho)]
        + [(m.start(), "e", m) for m in _RE_ITEM.finditer(trecho)],
        key=lambda t: t[0],
    )
    eventos, data = [], None
    for _, tipo, m in tokens:
        if tipo == "d":
            data = m.group(1)
            continue
        hora, usuario, situacao = m.group(1), (m.group(2) or "").strip(), m.group(3)
        if data is None or situacao in _NAO_SITUACAO:
            continue
        eventos.append({
            "quando": datetime.strptime(f"{data} {hora}", "%d/%m/%Y %H:%M"),
            "usuario": usuario,
            "situacao": situacao,
        })
    eventos.sort(key=lambda e: e["quando"])
    return eventos


_ROTULOS = [
    "Tarefa da Operação", "Operador da Tarefa", "Código de separação",
    "Estação de conferência", "Usuário da Estação da Conferência",
    "Inicio da Conferência", "Finalização da Conferência",
    "Tempo de Conclusão da Conferência", "Problema Reportado", "Expedição",
]
_FIM_VALOR = "|".join(re.escape(r) + ":" for r in _ROTULOS)


def _rotulo(texto: str, rotulo: str):
    """Valor depois de 'Rotulo:' ate o proximo rotulo conhecido; None se vazio ou 'Não informado'."""
    m = re.search(re.escape(rotulo) + r":\s*(.*?)\s*(?=" + _FIM_VALOR + r"|$)", texto)
    if not m:
        return None
    v = m.group(1).strip()
    return None if not v or v == _NAO_INFORMADO else v


def _data_hora(v):
    if v and _RE_DATA_HORA.match(v):
        return datetime.strptime(v, "%d/%m/%Y %H:%M")
    return None


def _segundos(v):
    m = re.match(r"^(\d+):(\d{2}):(\d{2})$", v or "")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3)) if m else None


def _numero(v: str) -> int:
    """Quantidade com virgula decimal brasileira ('1.250,5' -> 1250,5); arredonda ao inteiro mais proximo."""
    v = (v or "").strip().replace(".", "").replace(",", ".")
    try:
        return int(round(float(v)))
    except ValueError:
        return 0


def _itens(html: str) -> tuple:
    """(unidades, itens) da tabela 'Itens solicitados' (coluna Quantidade)."""
    i = html.find("Itens solicitados")
    if i < 0:
        return 0, 0
    fim = html.find("</table>", i)
    tabela = html[i:fim if fim > 0 else len(html)]
    unidades = itens = 0
    for linha in re.findall(r"<tr\b.*?</tr>", tabela, re.S):
        if "<td" not in linha:
            continue
        celulas = [_limpar(c) for c in re.findall(r"<td\b.*?</td>", linha, re.S)]
        if len(celulas) < 5:
            continue
        itens += 1
        unidades += _numero(celulas[4])
    return unidades, itens


def ler_historico(html: str) -> dict:
    html = html or ""
    texto = _limpar(html)
    estacao = _rotulo(texto, "Estação de conferência")
    usuario_conf = _rotulo(texto, "Usuário da Estação da Conferência")
    inicio = _data_hora(_rotulo(texto, "Inicio da Conferência"))
    fim = _data_hora(_rotulo(texto, "Finalização da Conferência"))
    segundos = _segundos(_rotulo(texto, "Tempo de Conclusão da Conferência"))
    problema = _rotulo(texto, "Problema Reportado") == "Sim"
    conferencia = None
    if estacao or usuario_conf or inicio or fim:
        conferencia = {
            "estacao": estacao, "usuario": usuario_conf, "inicio": inicio,
            "fim": fim, "segundos": segundos, "problema": problema,
        }
    unidades, itens = _itens(html)
    return {
        "linha_do_tempo": _linha_do_tempo(html),
        "operador_tarefa": _rotulo(texto, "Operador da Tarefa"),
        "tarefa": _rotulo(texto, "Tarefa da Operação"),
        "codigo_separacao": _rotulo(texto, "Código de separação"),
        "conferencia": conferencia,
        "unidades": unidades,
        "itens": itens,
    }
