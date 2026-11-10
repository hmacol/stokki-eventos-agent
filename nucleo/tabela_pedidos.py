"""
nucleo/tabela_pedidos.py

Tabela de TODOS os pedidos, no estilo da tela de Serviços da Vuupt
(Hugo, 09/11): uma linha por pedido, todas as informações que o núcleo
tem dele, filtro em qualquer coluna, ordenação, paginação e CSV.

Lê só o núcleo (dados/dados.db), nunca a Vuupt ao vivo -- mesma decisão
da /consulta (nucleo/consulta.py). O espelho dos serviços vem do
nucleo/sincronizar_servicos_vuupt.py (15 min) e o das rotas do
nucleo/sincronizar_vuupt.py (30 min), então a tela pode estar até ~15 min
atrás da Vuupt.

Rota, motorista, chegada e conclusão saem da ÚLTIMA parada do pedido
(a rota mais recente em que ele entrou). Pedido sem parada é pedido que
ainda não foi pra rota (pool, retirada, cancelado antes).

A tela é casca: as colunas, os filtros e a formatação moram aqui, pra o
CSV sair igual ao que está na tela.
"""
import csv
import io
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from nucleo import banco

POR_PAGINA = 100
DIAS_PADRAO = 7   # Hugo, 09/11: a tela abre nos últimos 7 dias

STATUS = [banco.PEDIDO_ABERTO, banco.PEDIDO_EM_ROTA, banco.PEDIDO_ENTREGUE,
          banco.PEDIDO_INSUCESSO, banco.PEDIDO_CANCELADO]
FLUXOS = [banco.FLUXO_ENTREGA, banco.FLUXO_RETIRADA]

# Qual data o filtro de período usa. "referencia" é a mesma ideia da
# /consulta: a data da rota, senão o agendamento, senão a criação --
# NUNCA só criado_em, que é quando a LINHA nasceu no núcleo (o backfill
# de 12/09 criou mil pedidos antigos com criado_em do dia).
DATAS = {
    "referencia": ("Rota / agendamento", "data_referencia"),
    "criacao": ("Criação na Vuupt", "date(COALESCE(criado_em_provedor, criado_em))"),
    "agendamento": ("Agendamento", "date(agendamento_inicio)"),
    "conclusao": ("Conclusão", "date(conclusao)"),
}

# O título é "#PS-1 - REFERENCIA / APELIDO / DESTINATARIO" (pipeline.py) ou
# "[RETIRADA] #PS-1 - REFERENCIA / ..." (retiradas/regras_retirada.py). A
# referência é o que vem depois do " - " no primeiro pedaço; olhar só o
# primeiro pedaço evita pegar o " - " de nome de embarcador sem apelido.
_SQL_CABECA = "substr(titulo, 1, instr(titulo || ' / ', ' / ') - 1)"
_SQL_REFERENCIA = (f"CASE WHEN instr({_SQL_CABECA}, ' - ') > 0 "
                   f"THEN trim(substr({_SQL_CABECA}, instr({_SQL_CABECA}, ' - ') + 3)) END")
# fluxo só existe desde 16/09; nas linhas antigas sai do título.
_SQL_FLUXO = "COALESCE(fluxo, CASE WHEN titulo LIKE '[RETIRADA]%' THEN 'RETIRADA' ELSE 'ENTREGA' END)"


@dataclass(frozen=True)
class Coluna:
    chave: str
    rotulo: str
    sql: str
    visivel: bool = False      # aparece sem o usuário escolher
    formato: str = "texto"     # texto | data | data_hora | status | numero


COLUNAS = [
    Coluna("data", "Data", "data_referencia", True, "data"),
    Coluna("codigo", "Código", "codigo", True),
    Coluna("referencia", "Referência", _SQL_REFERENCIA, True),
    Coluna("status", "Status", "status", True, "status"),
    Coluna("status_vuupt", "Status Vuupt",
           "TRIM(COALESCE(status_provedor, '') || CASE WHEN COALESCE(status_done_provedor, '') <> '' "
           "THEN ' / ' || status_done_provedor ELSE '' END)"),
    Coluna("fluxo", "Fluxo", _SQL_FLUXO, False, "status"),
    Coluna("reentrega_de", "Reentrega de", "reentrega_de_codigo"),
    Coluna("embarcador", "Embarcador", "remetente_nome", True),
    Coluna("embarcador_cnpj", "CNPJ embarcador", "remetente_codigo"),
    Coluna("destinatario", "Destinatário", "destinatario_nome", True),
    Coluna("destinatario_cnpj", "CNPJ/CPF destinatário", "destinatario_codigo"),
    Coluna("telefone", "Telefone", "destinatario_telefone"),
    Coluna("endereco", "Endereço", "endereco", True),
    Coluna("complemento", "Complemento", "complemento"),
    Coluna("zona", "Zona", "zona", True),
    Coluna("janela", "Janela do destinatário",
           "CASE WHEN horario_inicio IS NOT NULL OR horario_fim IS NOT NULL "
           "THEN COALESCE(horario_inicio, '') || ' às ' || COALESCE(horario_fim, '') END"),
    Coluna("agendamento", "Agendamento", "agendamento_inicio", True, "data_hora"),
    Coluna("agendamento_fim", "Agendamento até", "agendamento_fim", False, "data_hora"),
    Coluna("caixas", "Caixas", "caixas", True, "numero"),
    Coluna("data_rota", "Data da rota", "data_rota", False, "data"),
    Coluna("rota", "Rota", "rota_nome", True),
    Coluna("motorista", "Motorista", "motorista", True),
    Coluna("tipo_veiculo", "Veículo", "tipo_veiculo"),
    Coluna("ordem", "Ordem na rota", "ordem", False, "numero"),
    Coluna("situacao_parada", "Situação na rota", "situacao_parada", False, "status"),
    Coluna("chegada", "Chegada", "chegada", False, "data_hora"),
    Coluna("conclusao", "Conclusão", "conclusao", True, "data_hora"),
    Coluna("motivo", "Motivo do insucesso", "motivo", True),
    Coluna("tentativas", "Tentativas", "tentativas", False, "numero"),
    Coluna("canhoto", "Canhoto", "CASE WHEN COALESCE(qtd_checklists, 0) > 0 THEN 'Sim' ELSE 'Não' END"),
    Coluna("anexos", "Anexos", "qtd_anexos", False, "numero"),
    Coluna("origem", "Origem", "origem"),
    Coluna("nota", "Nota", "nota"),
    Coluna("criado", "Criado na Vuupt", "COALESCE(criado_em_provedor, criado_em)", False, "data_hora"),
    Coluna("atualizado", "Atualizado na Vuupt", "atualizado_em_provedor", False, "data_hora"),
    Coluna("excluido", "Excluído em", "excluido_em", False, "data_hora"),
    Coluna("service_id", "ID do serviço Vuupt", "vuupt_service_id", False, "numero"),
]
POR_CHAVE = {c.chave: c for c in COLUNAS}

# Última parada de cada pedido + a rota dela. ROW_NUMBER e não MAX():
# quero a LINHA inteira da parada mais recente, não o máximo de cada campo.
_SQL_BASE = """
WITH ult AS (
    SELECT p.codigo AS u_codigo, p.ordem AS ordem, p.situacao AS situacao_parada,
           p.arrived_at AS chegada, p.completed_at AS conclusao, p.motivo_texto AS motivo,
           p.tentativas AS tentativas, r.id AS rota_id, r.nome AS rota_nome, r.data_rota AS data_rota,
           r.agent_id AS agent_id, r.motorista_nome AS motorista, r.tipo_veiculo AS tipo_veiculo,
           ROW_NUMBER() OVER (PARTITION BY p.codigo ORDER BY r.data_rota DESC, p.id DESC) AS n
    FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
    WHERE p.codigo IS NOT NULL
),
base AS (
    SELECT pe.*, u.*,
           COALESCE(u.data_rota, date(pe.agendamento_inicio),
                    date(COALESCE(pe.criado_em_provedor, pe.criado_em))) AS data_referencia
    FROM nucleo_pedidos pe LEFT JOIN ult u ON u.u_codigo = pe.codigo AND u.n = 1
)
"""

_RE_DATA_BR = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$")


def _iso(valor) -> str | None:
    bruto = (str(valor) if valor is not None else "").strip()
    try:
        return date.fromisoformat(bruto).isoformat() if bruto else None
    except ValueError:
        return None


def periodo(de=None, ate=None) -> tuple[str, str]:
    """Sem data informada: últimos DIAS_PADRAO dias, contando hoje."""
    fim = _iso(ate) or date.today().isoformat()
    inicio = _iso(de) or (date.fromisoformat(fim) - timedelta(days=DIAS_PADRAO - 1)).isoformat()
    if inicio > fim:
        inicio, fim = fim, inicio
    return inicio, fim


def _termo_coluna(termo: str) -> str:
    """O banco guarda data em ISO; quem filtra digita 09/11 ou 09/11/2026."""
    m = _RE_DATA_BR.match(termo)
    if not m:
        return termo
    dia, mes, ano = m.group(1).zfill(2), m.group(2).zfill(2), m.group(3)
    return f"{ano}-{mes}-{dia}" if ano else f"-{mes}-{dia}"


def _where(filtros: dict) -> tuple[str, list]:
    """Monta o WHERE a partir do dicionário de filtros da tela. Chaves:
    de, ate, data_tipo, status, fluxo, sender_id, agent_id, zona, q e
    colunas = {chave_da_coluna: texto}."""
    inicio, fim = periodo(filtros.get("de"), filtros.get("ate"))
    _, sql_data = DATAS.get(filtros.get("data_tipo") or "", DATAS["referencia"])
    partes = [f"{sql_data} BETWEEN ? AND ?"]
    params: list = [inicio, fim]

    status = (filtros.get("status") or "").upper()
    if status in STATUS:
        partes.append("status = ?")
        params.append(status)
    fluxo = (filtros.get("fluxo") or "").upper()
    if fluxo in FLUXOS:
        partes.append(f"{_SQL_FLUXO} = ?")
        params.append(fluxo)
    for chave, sql in (("sender_id", "sender_id"), ("agent_id", "agent_id")):
        try:
            valor = int(filtros.get(chave) or 0)
        except (TypeError, ValueError):
            valor = 0
        if valor:
            partes.append(f"{sql} = ?")
            params.append(valor)
    if filtros.get("zona"):
        partes.append("zona = ?")
        params.append(filtros["zona"])

    q = (filtros.get("q") or "").strip().lstrip("#").strip()
    if q:
        campos = ["codigo", "titulo", "destinatario_nome", "destinatario_codigo", "remetente_nome",
                  "endereco", "zona", "motorista"]
        alvos = [f"%{q}%"]
        digitos = re.sub(r"\D", "", q)
        # CNPJ digitado com pontuação: o núcleo guarda só os dígitos.
        if re.fullmatch(r"[\d./\- ]+", q) and len(digitos) >= 4 and digitos != q:
            alvos.append(f"%{digitos}%")
        ou = []
        for alvo in alvos:
            ou += [f"{c} LIKE ?" for c in campos]
            params += [alvo] * len(campos)
        partes.append("(" + " OR ".join(ou) + ")")

    for chave, termo in (filtros.get("colunas") or {}).items():
        coluna, termo = POR_CHAVE.get(chave), (termo or "").strip()
        if coluna and termo:
            partes.append(f"CAST(({coluna.sql}) AS TEXT) LIKE ?")
            params.append(f"%{_termo_coluna(termo)}%")
    return " WHERE " + " AND ".join(partes), params


def _order_by(ordem: str | None, direcao: str | None) -> str:
    coluna = POR_CHAVE.get(ordem or "") or POR_CHAVE["data"]
    sentido = "ASC" if (direcao or "").lower() == "asc" else "DESC"
    # Vazio sempre por último, nos dois sentidos.
    return f" ORDER BY ({coluna.sql}) IS NULL, ({coluna.sql}) {sentido}, codigo {sentido}"


def _select() -> str:
    campos = ", ".join(f"({c.sql}) AS {c.chave}" for c in COLUNAS)
    return f"SELECT {campos}, rota_id FROM base"


def listar(filtros: dict, pagina: int = 1, por_pagina: int = POR_PAGINA, ordem: str | None = None,
           direcao: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        where, params = _where(filtros)
        tot = conn.execute(f"{_SQL_BASE} SELECT COUNT(*) n, COALESCE(SUM(caixas), 0) cx FROM base{where}",
                           params).fetchone()
        total = tot["n"]
        paginas = max(1, -(-total // por_pagina))
        pagina = min(max(1, int(pagina or 1)), paginas)
        rows = conn.execute(f"{_SQL_BASE} {_select()}{where}{_order_by(ordem, direcao)} LIMIT ? OFFSET ?",
                            params + [por_pagina, (pagina - 1) * por_pagina]).fetchall()
        inicio, fim = periodo(filtros.get("de"), filtros.get("ate"))
        return {"linhas": [dict(r) for r in rows], "total": total, "total_caixas": tot["cx"],
                "pagina": pagina, "paginas": paginas, "periodo": {"de": inicio, "ate": fim}}
    finally:
        if fechar:
            conn.close()


def opcoes(conn: sqlite3.Connection | None = None) -> dict:
    """Listas das caixas de seleção: embarcadores, motoristas e zonas que
    aparecem no núcleo (em qualquer data -- a lista é curta)."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        embarcadores = [dict(r) for r in conn.execute("""
            SELECT sender_id, MAX(remetente_nome) AS nome FROM nucleo_pedidos
            WHERE sender_id IS NOT NULL AND COALESCE(remetente_nome, '') <> ''
            GROUP BY sender_id ORDER BY nome""")]
        motoristas = [dict(r) for r in conn.execute("""
            SELECT agent_id, MAX(motorista_nome) AS nome FROM nucleo_rotas
            WHERE agent_id IS NOT NULL AND COALESCE(motorista_nome, '') <> ''
            GROUP BY agent_id ORDER BY nome""")]
        zonas = [r[0] for r in conn.execute(
            "SELECT DISTINCT zona FROM nucleo_pedidos WHERE COALESCE(zona, '') <> '' ORDER BY zona")]
        return {"embarcadores": embarcadores, "motoristas": motoristas, "zonas": zonas}
    finally:
        if fechar:
            conn.close()


def formatar(chave: str, valor) -> str:
    """Valor pronto pra tela e pro CSV (data em dd/mm/aaaa)."""
    if valor is None or valor == "":
        return ""
    formato = POR_CHAVE[chave].formato if chave in POR_CHAVE else "texto"
    texto = str(valor)
    if formato == "data" and len(texto) >= 10:
        return f"{texto[8:10]}/{texto[5:7]}/{texto[:4]}"
    if formato == "data_hora" and len(texto) >= 10:
        hora = texto[11:16]
        return f"{texto[8:10]}/{texto[5:7]}/{texto[:4]}" + (f" {hora}" if hora else "")
    if formato == "status":
        return texto.replace("_", " ").capitalize()
    return texto


def exportar_csv(filtros: dict, chaves: list[str] | None = None, ordem: str | None = None,
                 direcao: str | None = None, conn: sqlite3.Connection | None = None) -> str:
    """CSV de TUDO que o filtro pega (não só a página aberta), nas colunas
    pedidas (padrão: todas). Separador ';' e BOM: é o que o Excel em
    português abre direto, sem assistente de importação."""
    colunas = [POR_CHAVE[k] for k in (chaves or []) if k in POR_CHAVE] or COLUNAS
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        where, params = _where(filtros)
        rows = conn.execute(f"{_SQL_BASE} {_select()}{where}{_order_by(ordem, direcao)}", params).fetchall()
    finally:
        if fechar:
            conn.close()
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow([c.rotulo for c in colunas])
    for r in rows:
        escritor.writerow([_celula_csv(c, formatar(c.chave, r[c.chave])) for c in colunas])
    return "﻿" + saida.getvalue()


def _celula_csv(coluna: Coluna, texto: str) -> str:
    """Texto livre (destinatário, observação, motivo...) que começa com =, +, -, @, tab ou CR o Excel
    executa como fórmula: um apóstrofo na frente o mantém como texto (revisão de segurança, 09/11)."""
    if coluna.formato == "texto" and texto[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + texto
    return texto
