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

from nucleo import banco, canhotos_manuais

POR_PAGINA = 100
DIAS_PADRAO = 7   # Hugo, 09/11: a tela abre nos últimos 7 dias

# IMPORTADO (Hugo, 12/11): pedido que já está na Stokki (ou sendo enviado
# pelo portal) e ainda não chegou à Vuupt. Não existe em nucleo_pedidos: só
# esta tela monta, juntando galpao_pedidos e portal_envios (ver _norm).
IMPORTADO = "IMPORTADO"
STATUS = [IMPORTADO, banco.PEDIDO_ABERTO, banco.PEDIDO_EM_ROTA, banco.PEDIDO_ENTREGUE,
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
# Tem canhoto (Hugo, 11/11): o mesmo critério da /canhotos-pendentes
# (canhotos_manuais.pendentes) -- foto no checklist da Vuupt, canhoto ou
# assinatura do app (nucleo_comprovantes da última parada) ou canhoto subido
# pelo painel (canhotos_manuais). Vale pra coluna Canhoto e pro filtro.
_SQL_TEM_CANHOTO = (
    "(COALESCE(base.qtd_checklists, 0) > 0"
    " OR base.c IN (SELECT UPPER(LTRIM(codigo, '# ')) FROM canhotos_manuais)"
    " OR EXISTS (SELECT 1 FROM nucleo_comprovantes cp WHERE cp.parada_id = base.parada_id"
    " AND cp.tipo IN ('CANHOTO', 'ASSINATURA')))")


@dataclass(frozen=True)
class Coluna:
    chave: str
    rotulo: str
    sql: str
    visivel: bool = False      # aparece sem o usuário escolher
    formato: str = "texto"     # texto | data | data_hora | status | numero


COLUNAS = [
    Coluna("data", "Data", "data_referencia", True, "data"),
    Coluna("codigo", "Código", "pedido_base", True),
    Coluna("codigo_servico", "Código do serviço", "codigo"),
    Coluna("referencia", "Referência", _SQL_REFERENCIA, True),
    Coluna("status", "Status", "status", True, "status"),
    Coluna("situacao_stokki", "Situação na Stokki", "situacao_stokki"),
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
    Coluna("canhoto", "Canhoto", f"CASE WHEN {_SQL_TEM_CANHOTO} THEN 'Sim' ELSE 'Não' END"),
    Coluna("anexos", "Anexos", "qtd_anexos", False, "numero"),
    Coluna("origem", "Origem", "origem"),
    Coluna("nota", "Nota", "nota"),
    Coluna("criado", "Criado na Vuupt", "COALESCE(criado_em_provedor, criado_em)", False, "data_hora"),
    Coluna("atualizado", "Atualizado na Vuupt", "atualizado_em_provedor", False, "data_hora"),
    Coluna("excluido", "Excluído em", "excluido_em", False, "data_hora"),
    Coluna("service_id", "ID do serviço Vuupt", "vuupt_service_id", False, "numero"),
]
POR_CHAVE = {c.chave: c for c in COLUNAS}

# Posição do primeiro "-R<dígito>" do código normalizado (reentrega); 9999 se não tem.
_SQL_POS_R = "MIN(" + ", ".join(
    f"COALESCE(NULLIF(instr(pe.c, '-R{d}'), 0), 9999)" for d in range(10)) + ")"

# Última parada de cada pedido + a rota dela. ROW_NUMBER e não MAX():
# quero a LINHA inteira da parada mais recente, não o máximo de cada campo.
# base_raw é uma linha por SERVIÇO (original e cada reentrega); `base` é uma
# linha por PEDIDO (Hugo, 10/11): original e reentregas (#PS-1, #PS-1-R1...)
# viram uma linha só, a da tentativa mais recente -- mesma regra do portal
# (portal_cliente/dados_cliente.unir_tentativas). A chave do grupo é
# (empresa, código base): duas empresas com o mesmo código não se misturam.
_SQL_CTES = f"""
WITH ult AS (
    SELECT p.codigo AS u_codigo, p.id AS parada_id, p.ordem AS ordem, p.situacao AS situacao_parada,
           p.arrived_at AS chegada, p.completed_at AS conclusao, p.motivo_texto AS motivo,
           p.tentativas AS tentativas, r.id AS rota_id, r.nome AS rota_nome, r.data_rota AS data_rota,
           r.agent_id AS agent_id, r.motorista_nome AS motorista, r.tipo_veiculo AS tipo_veiculo,
           ROW_NUMBER() OVER (PARTITION BY p.codigo ORDER BY r.data_rota DESC, p.id DESC) AS n
    FROM nucleo_paradas p JOIN nucleo_rotas r ON r.id = p.rota_id
    WHERE p.codigo IS NOT NULL
),
norm AS (
    SELECT t.*, UPPER(LTRIM(t.codigo, '# ')) AS c FROM (__NORM__) t
),
base_raw AS (
    SELECT pe.*, u.*,
           CASE WHEN {_SQL_POS_R} = 9999 THEN pe.c ELSE substr(pe.c, 1, {_SQL_POS_R} - 1) END AS pedido_base,
           CASE WHEN {_SQL_POS_R} = 9999 THEN 0
                ELSE CAST(substr(pe.c, {_SQL_POS_R} + 2) AS INTEGER) END AS sufixo_r,
           CASE pe.status WHEN 'ENTREGUE' THEN 5 WHEN 'EM_ROTA' THEN 3 WHEN 'ABERTO' THEN 2
                          WHEN 'INSUCESSO' THEN 1 ELSE 0 END AS peso,
           COALESCE(u.data_rota, date(pe.agendamento_inicio),
                    date(COALESCE(pe.criado_em_provedor, pe.criado_em))) AS data_referencia
    FROM norm pe LEFT JOIN ult u ON u.u_codigo = pe.codigo AND u.n = 1
)
"""
# Campos do pedido que a tela usa, na ordem em que as três fontes do `norm`
# devolvem (núcleo, Stokki pelo coletor do galpão, envio do portal).
_CAMPOS = ["codigo", "vuupt_service_id", "titulo", "status", "sender_id", "remetente_nome",
           "remetente_codigo", "destinatario_nome", "destinatario_codigo", "destinatario_telefone",
           "endereco", "complemento", "zona", "horario_inicio", "horario_fim", "agendamento_inicio",
           "agendamento_fim", "caixas", "fluxo", "reentrega_de_codigo", "status_provedor",
           "status_done_provedor", "qtd_checklists", "qtd_anexos", "origem", "nota",
           "criado_em_provedor", "criado_em", "atualizado_em_provedor", "excluido_em", "situacao_stokki"]
_NORM_NUCLEO = "SELECT " + ", ".join(_CAMPOS[:-1]) + ", NULL AS situacao_stokki FROM nucleo_pedidos"
# Envio do portal que vale como pedido: o último de cada código (ou o próprio
# envio, enquanto não tem código). Erro e duplicado ficam fora (Hugo, 12/11).
_ENVIOS = ("(SELECT * FROM portal_envios WHERE id IN (SELECT MAX(id) FROM portal_envios "
           "WHERE status NOT IN ('ERRO', 'DUPLICADO') GROUP BY COALESCE(codigo_pedido, 'ENVIO-' || id)))")
_ENDERECO_ENVIO = ("NULLIF(TRIM(COALESCE(e.destinatario_endereco, '') || ' ' || COALESCE(e.destinatario_bairro, '')"
                   " || ' ' || COALESCE(e.destinatario_municipio, '') || ' ' || COALESCE(e.destinatario_uf, '')), '')")
_AGENDA_ENVIO = "e.agendamento_data || ' ' || COALESCE(e.agendamento_hora_inicio, '00:00')"
_TITULO_ENVIO = "'#' || {cod} || CASE WHEN e.numero_nf IS NOT NULL THEN ' - ' || e.numero_nf ELSE '' END"
_DO_ENVIO = {
    "destinatario_nome": "e.destinatario_nome", "destinatario_codigo": "e.destinatario_doc",
    "destinatario_telefone": "e.destinatario_telefone", "endereco": _ENDERECO_ENVIO,
    "horario_inicio": "e.horario_inicio", "horario_fim": "e.horario_fim",
    "agendamento_inicio": _AGENDA_ENVIO, "caixas": "e.volumes", "nota": "e.observacoes"}


def _fora_do_nucleo(codigo: str) -> str:
    return f"NOT EXISTS (SELECT 1 FROM nucleo_pedidos n WHERE n.codigo IN ({codigo}, '#' || {codigo}))"


def _campos(valores: dict) -> str:
    """SELECT na ordem de _CAMPOS; o que não está em `valores` sai NULL."""
    return "SELECT " + ", ".join(f"{valores.get(c, 'NULL')} AS {c}" for c in _CAMPOS)


def _norm(conn: sqlite3.Connection) -> str:
    """As linhas de pedido da tela: o núcleo (espelho da Vuupt) mais o que
    ainda não chegou lá -- pedidos da Stokki vistos pelo coletor do galpão
    (galpao_pedidos, a cada minuto) e envios do portal que o coletor ainda
    não viu. Cancelado na Stokki/portal entra como CANCELADO; o resto, como
    IMPORTADO. Some daqui sozinho quando o código aparece no núcleo. Só usa
    as tabelas que existem (banco de teste pode não ter galpão nem portal)."""
    tabelas = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
        "('galpao_pedidos', 'galpao_eventos', 'portal_envios', 'clientes_portal')")}
    galpao = {"galpao_pedidos", "galpao_eventos"} <= tabelas
    portal = {"portal_envios", "clientes_portal"} <= tabelas
    partes = [_NORM_NUCLEO]
    if galpao:
        do_envio = dict(_DO_ENVIO, titulo=_TITULO_ENVIO.format(cod="g.codigo")) if portal else {}
        ou = (lambda sql: f", {sql}") if portal else (lambda sql: "")
        partes.append(_campos({
            **do_envio,
            "codigo": "g.codigo",
            "status": ("CASE WHEN g.situacao = 'Cancelado'" + (" OR e.status = 'CANCELADO'" if portal else "")
                       + f" THEN 'CANCELADO' ELSE '{IMPORTADO}' END"),
            "sender_id": "COALESCE(m.e_sid" + ou("cp.sender_id") + ", NULL)",
            "remetente_nome": "COALESCE(m.e_rem" + ou("cp.nome") + ", g.embarcador)",
            "remetente_codigo": "COALESCE(m.e_cnpj" + ou("e.cnpj_embarcador") + ", NULL)",
            "origem": "'STOKKI'",
            "criado_em": ("COALESCE((SELECT MIN(ev.quando) FROM galpao_eventos ev WHERE ev.id_stokki = g.id_stokki)"
                          + ou("e.criado_em") + ", g.atualizado_em)"),
            "situacao_stokki": "g.situacao"})
            + " FROM galpao_pedidos g"
            # embarcador do galpão ("NOME ... #stkkc-N") -> o da Vuupt, pelo pedido mais novo dele no núcleo
            + " LEFT JOIN (SELECT g2.embarcador AS e_nome, n.sender_id AS e_sid, n.remetente_nome AS e_rem,"
              " n.remetente_codigo AS e_cnpj, MAX(g2.id_stokki) AS e_ultimo FROM galpao_pedidos g2"
              " JOIN nucleo_pedidos n ON n.codigo = g2.codigo WHERE n.sender_id IS NOT NULL"
              " GROUP BY g2.embarcador) m ON m.e_nome = g.embarcador"
            + (f" LEFT JOIN {_ENVIOS} e ON e.codigo_pedido = g.codigo"
               " LEFT JOIN clientes_portal cp ON cp.cnpj = e.cnpj_embarcador" if portal else "")
            + f" WHERE g.codigo IS NOT NULL AND {_fora_do_nucleo('g.codigo')}")
    if portal:
        cod = "COALESCE(e.codigo_pedido, 'ENVIO-' || e.id)"
        partes.append(_campos({
            **_DO_ENVIO,
            "codigo": cod, "titulo": _TITULO_ENVIO.format(cod=cod),
            "status": f"CASE WHEN e.status = 'CANCELADO' THEN 'CANCELADO' ELSE '{IMPORTADO}' END",
            "sender_id": "cp.sender_id", "remetente_nome": "cp.nome", "remetente_codigo": "e.cnpj_embarcador",
            "origem": "'PORTAL'", "criado_em": "e.criado_em",
            "situacao_stokki": ("CASE e.status WHEN 'CRIADO' THEN 'Criado' WHEN 'CANCELADO' THEN 'Cancelado' "
                                "WHEN 'EM_ESPERA_CLIENTE' THEN 'Em espera a pedido do cliente' "
                                "ELSE 'Enviando pelo portal' END")})
            + f" FROM {_ENVIOS} e LEFT JOIN clientes_portal cp ON cp.cnpj = e.cnpj_embarcador"
            + " WHERE (e.codigo_pedido IS NULL OR (" + _fora_do_nucleo("e.codigo_pedido")
            + (" AND NOT EXISTS (SELECT 1 FROM galpao_pedidos g WHERE g.codigo = e.codigo_pedido)" if galpao else "")
            + "))")
    return " UNION ALL ".join(partes)


# Linha principal do grupo: 1) reentrega VIVA (aberto/em rota) com -R maior que o da tentativa
# entregue -- é um reenvio feito depois da entrega e não pode ficar escondido (revisão final, 10/11);
# 2) entregue (vence tentativa fechada e tentativa mais antiga); depois a data, o peso do status
# (empate no mesmo dia), o número da reentrega e o código.
# `alvo` (12/11, velocidade): só os pedidos com ALGUMA tentativa na data do
# filtro entram no agrupamento. A linha principal do pedido é uma das
# tentativas dele, então quem passaria no filtro de data está sempre em `alvo`
# -- o resultado é o mesmo, mas as janelas rodam em centenas de linhas e não
# no núcleo inteiro (em produção: de ~0,9 s pra ~0,1 s por consulta).
# __DATA__ é a expressão da data escolhida (DATAS); ver _consulta().
_SQL_BASE = _SQL_CTES.rstrip() + """,
alvo AS (
    SELECT DISTINCT COALESCE(sender_id, -1) AS a_sid, pedido_base AS a_pb
    FROM base_raw WHERE __DATA__ BETWEEN ? AND ?
),
com_entregue AS (
    SELECT base_raw.*,
           MAX(CASE WHEN status = 'ENTREGUE' THEN sufixo_r END)
               OVER (PARTITION BY COALESCE(sender_id, -1), pedido_base) AS sufixo_r_entregue
    FROM base_raw
    WHERE (COALESCE(sender_id, -1), pedido_base) IN (SELECT a_sid, a_pb FROM alvo)
),
rank AS (
    SELECT com_entregue.*,
           ROW_NUMBER() OVER (PARTITION BY COALESCE(sender_id, -1), pedido_base
                              ORDER BY COALESCE(status IN ('ABERTO', 'EM_ROTA')
                                                AND sufixo_r > sufixo_r_entregue, 0) DESC,
                                       (status = 'ENTREGUE') DESC, data_referencia DESC, peso DESC,
                                       sufixo_r DESC, codigo) AS rn_grupo,
           group_concat(codigo, ' ') OVER (PARTITION BY COALESCE(sender_id, -1), pedido_base) AS codigos_grupo
    FROM com_entregue
),
base AS (SELECT * FROM rank WHERE rn_grupo = 1)
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
    de, ate, data_tipo, status, fluxo, sender_id, agent_id, zona, canhoto
    (com | sem), q e colunas = {chave_da_coluna: texto}."""
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
    canhoto = (filtros.get("canhoto") or "").lower()
    if canhoto == "com":
        partes.append(_SQL_TEM_CANHOTO)
    elif canhoto == "sem":
        # Só entregue (Hugo, 11/11), e checklist sem informação (NULL) fica
        # fora, como na /canhotos-pendentes.
        partes.append(f"status = 'ENTREGUE' AND qtd_checklists = 0 AND NOT {_SQL_TEM_CANHOTO}")

    q = (filtros.get("q") or "").strip().lstrip("#").strip()
    if q:
        campos = ["codigo", "titulo", "destinatario_nome", "destinatario_codigo", "remetente_nome",
                  "endereco", "zona", "motorista", "codigos_grupo"]
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


def _consulta(filtros: dict, conn: sqlite3.Connection) -> tuple[str, str, list]:
    """(CTEs até `base`, WHERE, parâmetros) pros filtros da tela. O período
    entra duas vezes: no `alvo` (quem participa do agrupamento) e no WHERE
    (a linha principal do pedido)."""
    where, params = _where(filtros)
    _, sql_data = DATAS.get(filtros.get("data_tipo") or "", DATAS["referencia"])
    return (_SQL_BASE.replace("__DATA__", sql_data).replace("__NORM__", _norm(conn)), where,
            params[:2] + params)


def _order_by(ordem: str | None, direcao: str | None) -> str:
    coluna = POR_CHAVE.get(ordem or "") or POR_CHAVE["data"]
    sentido = "ASC" if (direcao or "").lower() == "asc" else "DESC"
    # Vazio sempre por último, nos dois sentidos.
    return f" ORDER BY ({coluna.sql}) IS NULL, ({coluna.sql}) {sentido}, codigo {sentido}"


def _select(extra: str = "") -> str:
    campos = ", ".join(f"({c.sql}) AS {c.chave}" for c in COLUNAS)
    return f"SELECT {campos}, rota_id, sender_id, pedido_base, vuupt_service_id{extra} FROM base"


def listar(filtros: dict, pagina: int = 1, por_pagina: int = POR_PAGINA, ordem: str | None = None,
           direcao: str | None = None, conn: sqlite3.Connection | None = None) -> dict:
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        canhotos_manuais.garantir_tabela(conn)   # a coluna Canhoto lê dela
        base, where, params = _consulta(filtros, conn)
        # Total e caixas vêm na mesma consulta da página (12/11, velocidade).
        sql = (f"{base} {_select(', COUNT(*) OVER () AS _n, COALESCE(SUM(caixas) OVER (), 0) AS _cx')}"
               f"{where}{_order_by(ordem, direcao)} LIMIT ? OFFSET ?")
        pagina = max(1, int(pagina or 1))
        rows = conn.execute(sql, params + [por_pagina, (pagina - 1) * por_pagina]).fetchall()
        if not rows and pagina > 1:
            # Página além do fim (filtro mudou com a URL antiga): cai na última.
            n = conn.execute(f"{base} SELECT COUNT(*) n FROM base{where}", params).fetchone()["n"]
            pagina = max(1, -(-n // por_pagina))
            rows = conn.execute(sql, params + [por_pagina, (pagina - 1) * por_pagina]).fetchall()
        total, caixas = (rows[0]["_n"], rows[0]["_cx"]) if rows else (0, 0)
        paginas = max(1, -(-total // por_pagina))
        inicio, fim = periodo(filtros.get("de"), filtros.get("ate"))
        linhas = [{k: r[k] for k in r.keys() if k not in ("_n", "_cx")} for r in rows]
        hist = tentativas([(l["sender_id"], l["pedido_base"]) for l in linhas], conn)
        for l in linhas:
            l["historico"] = hist.get((l["sender_id"], l["pedido_base"]), [])
        return {"linhas": linhas, "total": total, "total_caixas": caixas,
                "pagina": pagina, "paginas": paginas, "periodo": {"de": inicio, "ate": fim}}
    finally:
        if fechar:
            conn.close()


def contar_por_status(filtros: dict, conn: sqlite3.Connection | None = None) -> dict:
    """Abas da tela no celular (Hugo, 11/11): quantos pedidos em cada status
    com os mesmos filtros da tela, menos o de status (a aba é o status)."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        canhotos_manuais.garantir_tabela(conn)   # a coluna Canhoto lê dela
        base, where, params = _consulta({**filtros, "status": ""}, conn)
        rows = conn.execute(f"{base} SELECT status, COUNT(*) n FROM base{where} GROUP BY status",
                            params).fetchall()
    finally:
        if fechar:
            conn.close()
    saida = {s: 0 for s in STATUS}
    for r in rows:
        if r["status"] in saida:
            saida[r["status"]] = r["n"]
    saida["total"] = sum(r["n"] for r in rows)
    return saida


def contar_filas(filtros: dict, conn: sqlite3.Connection | None = None) -> dict:
    """Filas do topo da tela no computador (Hugo, 11/11): quantos pedidos por
    status e quantos entregues sem canhoto, com os filtros da tela MENOS status
    e canhoto (a fila é quem escolhe os dois). Uma consulta só."""
    fechar = conn is None
    conn = conn or banco.conectar()
    try:
        canhotos_manuais.garantir_tabela(conn)
        base, where, params = _consulta({**filtros, "status": "", "canhoto": ""}, conn)
        rows = conn.execute(
            f"{base} SELECT status, COUNT(*) n, COALESCE(SUM(CASE WHEN status = 'ENTREGUE' "
            f"AND qtd_checklists = 0 AND NOT {_SQL_TEM_CANHOTO} THEN 1 ELSE 0 END), 0) sc "
            f"FROM base{where} GROUP BY status", params).fetchall()
    finally:
        if fechar:
            conn.close()
    saida = {s: 0 for s in STATUS}
    for r in rows:
        if r["status"] in saida:
            saida[r["status"]] = r["n"]
    saida["total"] = sum(r["n"] for r in rows)
    saida["sem_canhoto"] = sum(r["sc"] for r in rows)
    return saida


def tentativas(chaves: list[tuple[int | None, str]], conn: sqlite3.Connection) -> dict:
    """Histórico de cada pedido (chave = (sender_id, código base)): as paradas
    que terminaram (entregue, parcial, insucesso -- "não coletado" entra como
    qualquer insucesso; remanejo/cancelada não é tentativa) mais um item por
    serviço aberto, em rota ou cancelado, que é o estado atual dele. Em ordem
    de data."""
    saida: dict = {}
    chaves = list(dict.fromkeys((s, b) for s, b in chaves))
    for i in range(0, len(chaves), 500):
        bloco = chaves[i:i + 500]
        alvo = ", ".join(["(?, ?)"] * len(bloco))
        params = [x for s, b in bloco for x in (-1 if s is None else s, b)]
        ctes = _SQL_CTES.rstrip().replace("__NORM__", _norm(conn))
        rows = conn.execute(f"""{ctes},
            alvo(sid, pb) AS (VALUES {alvo})
            SELECT br.sender_id AS sender_id, br.pedido_base AS pedido_base, r.data_rota AS data,
                   br.codigo AS codigo, p.situacao AS situacao, p.motivo_texto AS motivo,
                   r.nome AS rota, r.id AS rota_id, r.motorista_nome AS motorista,
                   p.completed_at AS conclusao
            FROM base_raw br JOIN alvo ON alvo.sid = COALESCE(br.sender_id, -1) AND alvo.pb = br.pedido_base
            JOIN nucleo_paradas p ON p.codigo = br.codigo
            JOIN nucleo_rotas r ON r.id = p.rota_id
            WHERE p.situacao IN ('ENTREGUE', 'PARCIAL', 'INSUCESSO')
            UNION ALL
            SELECT br.sender_id, br.pedido_base, br.data_referencia, br.codigo, br.status, NULL,
                   CASE WHEN br.status = 'EM_ROTA' THEN br.rota_nome END,
                   CASE WHEN br.status = 'EM_ROTA' THEN br.rota_id END,
                   CASE WHEN br.status = 'EM_ROTA' THEN br.motorista END, NULL
            FROM base_raw br JOIN alvo ON alvo.sid = COALESCE(br.sender_id, -1) AND alvo.pb = br.pedido_base
            WHERE br.status IN ('IMPORTADO', 'ABERTO', 'EM_ROTA', 'CANCELADO')""", params).fetchall()
        vistos = set()
        for r in rows:
            item = {k: r[k] for k in ("data", "codigo", "situacao", "motivo", "rota", "rota_id",
                                      "motorista", "conclusao")}
            chave = (r["sender_id"], r["pedido_base"])
            dedup = (chave, item["data"], item["codigo"], item["situacao"], item["motivo"])
            if dedup not in vistos:
                vistos.add(dedup)
                saida.setdefault(chave, []).append(item)
    for lista in saida.values():
        lista.sort(key=lambda t: t["data"] or "")
    return saida


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
        canhotos_manuais.garantir_tabela(conn)   # a coluna Canhoto lê dela
        base, where, params = _consulta(filtros, conn)
        rows = conn.execute(f"{base} {_select()}{where}{_order_by(ordem, direcao)}", params).fetchall()
        hist = tentativas([(r["sender_id"], r["pedido_base"]) for r in rows], conn)
    finally:
        if fechar:
            conn.close()
    saida = io.StringIO()
    escritor = csv.writer(saida, delimiter=";")
    escritor.writerow([c.rotulo for c in colunas] + ["Histórico de tentativas"])
    texto_livre = Coluna("historico", "Histórico de tentativas", "")
    for r in rows:
        itens = hist.get((r["sender_id"], r["pedido_base"]), [])
        escritor.writerow([_celula_csv(c, formatar(c.chave, r[c.chave])) for c in colunas]
                          + [_celula_csv(texto_livre, "; ".join(_texto_tentativa(t) for t in itens))])
    return "﻿" + saida.getvalue()


def _texto_tentativa(t: dict) -> str:
    """"dd/mm Situação (motivo)", o mesmo formato do xlsx do portal."""
    dia = t["data"] or ""
    texto = f"{dia[8:10]}/{dia[5:7]} " if len(dia) >= 10 else ""
    texto += formatar("status", t["situacao"])
    return texto + (f" ({t['motivo']})" if t["motivo"] else "")


def _celula_csv(coluna: Coluna, texto: str) -> str:
    """Texto livre (destinatário, observação, motivo...) que começa com =, +, -, @, tab ou CR o Excel
    executa como fórmula: um apóstrofo na frente o mantém como texto (revisão de segurança, 09/11)."""
    if coluna.formato == "texto" and texto[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + texto
    return texto
