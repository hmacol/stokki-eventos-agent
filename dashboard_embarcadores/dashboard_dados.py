# -*- coding: utf-8 -*-
"""
dashboard_dados.py

Coleta e cache dos dados do dashboard web de comparativo de embarcadores
(pedido do Hugo, 30/07): pedidos por semana desde o início do ano,
fluxo mensal por embarcador, destinatários mais recorrentes -- e os
filtros interativos pedidos em seguida (30/07): destinatários
recorrentes por ano/mês, pedidos por semana filtrado por embarcador,
pedidos por embarcador filtrado por ano/mês/semana.

Estratégia de cache mensal (sugestão do Hugo): todo mês FECHADO é
buscado uma ÚNICA vez e salvo permanentemente em
dados/cache_mensal/AAAA-MM.json — nunca mais muda. Só o mês CORRENTE é
buscado fresco a cada atualização.

Dois níveis de dado ficam salvos (por atualizar_dashboard.py):
  - dashboard_atual.json — resumo pronto (cards, gráfico semanal,
    tabela mensal por embarcador, top recorrentes) — o que a página
    carrega de cara.
  - registros_dashboard.json — lista ENRIQUECIDA (embarcador já
    resolvido/unificado por CNPJ, ano/mês/semana derivados) — usada
    pelas rotas de filtro (/api/...) pra agregar sob demanda, sem
    precisar guardar uma versão pré-calculada de cada combinação
    possível de filtro.

Uso (chamado por atualizar_dashboard.py, 1x/dia):
    from vuupt_client import VuuptClient
    from dashboard_dados import atualizar_cache, montar_dashboard

    vuupt = VuuptClient(token)
    registros = atualizar_cache(vuupt)
    resumo, enriquecidos = montar_dashboard(registros)
"""
import json
import logging
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
PASTA_CACHE = _RAIZ_LOCAL / "dados" / "cache_mensal"
DB_PATH     = _RAIZ_PROJETO / "dados" / "dados.db"


def _primeiro_dia_mes(ano: int, mes: int) -> date:
    return date(ano, mes, 1)


def _primeiro_dia_proximo_mes(ano: int, mes: int) -> date:
    if mes == 12:
        return date(ano + 1, 1, 1)
    return date(ano, mes + 1, 1)


def _chave_mes(ano: int, mes: int) -> str:
    return f"{ano:04d}-{mes:02d}"


def _mes_esta_fechado(ano: int, mes: int, referencia: date) -> bool:
    """Um mês está fechado (pode ser cacheado pra sempre) se já não é
    mais o mês/ano da data de referência (hoje)."""
    return (ano, mes) != (referencia.year, referencia.month)


def _buscar_registros_mes(vuupt, ano: int, mes: int) -> list[dict]:
    """
    Busca os registros REDUZIDOS (só os campos usados nas agregações)
    de todos os serviços com scheduled_start no mês dado.
    """
    inicio = _primeiro_dia_mes(ano, mes)
    fim = _primeiro_dia_proximo_mes(ano, mes)
    filtro = [
        {"field": "scheduled_start", "operator": "gte", "value": inicio.strftime("%Y-%m-%d")},
        {"field": "scheduled_start", "operator": "lt", "value": fim.strftime("%Y-%m-%d")},
    ]
    servicos = vuupt.listar_servicos(filtro, per_page=100)
    reduzidos = []
    for s in servicos:
        data_str = (s.get("scheduled_start") or "")[:10]
        if not data_str:
            continue
        reduzidos.append({
            "data": data_str,
            "sender_id": s.get("sender_id"),
            "customer_id": s.get("customer_id"),
            "title": s.get("title") or "",
            "status": s.get("status"),
        })
    return reduzidos


def atualizar_cache(vuupt, referencia: date | None = None) -> list[dict]:
    """
    Garante que todo mês de janeiro até o mês anterior ao atual esteja
    cacheado (busca só 1x na vida de cada mês fechado) e sempre busca o
    mês ATUAL fresco. Retorna a lista combinada de registros do ano
    inteiro (janeiro até hoje).
    """
    referencia = referencia or date.today()
    PASTA_CACHE.mkdir(parents=True, exist_ok=True)

    todos_registros = []
    for mes in range(1, referencia.month + 1):
        ano = referencia.year
        chave = _chave_mes(ano, mes)
        caminho_cache = PASTA_CACHE / f"{chave}.json"
        fechado = _mes_esta_fechado(ano, mes, referencia)

        if fechado and caminho_cache.exists():
            registros = json.loads(caminho_cache.read_text(encoding="utf-8"))
            logger.info(f"Mês {chave}: usando cache ({len(registros)} registro(s)).")
        else:
            logger.info(f"Mês {chave}: buscando do VUUPT ({'fechando agora' if fechado else 'mês corrente'})...")
            registros = _buscar_registros_mes(vuupt, ano, mes)
            if fechado:
                caminho_cache.write_text(
                    json.dumps(registros, ensure_ascii=False), encoding="utf-8"
                )
                logger.info(f"Mês {chave}: {len(registros)} registro(s) — cacheado permanentemente.")
            else:
                logger.info(f"Mês {chave}: {len(registros)} registro(s) (não cacheado — ainda em andamento).")

        todos_registros.extend(registros)

    return todos_registros


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _semana_iso(data_str: str) -> str:
    d = datetime.strptime(data_str, "%Y-%m-%d").date()
    ano_iso, semana_iso, _ = d.isocalendar()
    return f"{ano_iso}-W{semana_iso:02d}"


def _carregar_mapa_embarcadores() -> dict:
    """
    {sender_id: nome_canônico} a partir da tabela interno.

    Agrupa por CNPJ do embarcador — a base tem vários sender_id/perfis
    diferentes no VUUPT pro MESMO CNPJ (contas criadas em momentos
    diferentes, às vezes com apelido levemente diferente), o que
    poluía o comparativo mostrando o mesmo embarcador várias vezes
    (confirmado pelo Hugo, 30/07). Todo sender_id que compartilha um
    CNPJ usa o MESMO nome (o primeiro nome não vazio encontrado pra
    aquele CNPJ na tabela).
    """
    if not DB_PATH.exists():
        return {}
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT sender_id, cnpj_embarcador, apelido, nome_remetente "
            "FROM interno WHERE sender_id IS NOT NULL"
        ).fetchall()
        conn.close()
    except Exception as e:
        logger.debug(f"Erro ao carregar mapa de embarcadores: {e}")
        return {}

    nome_por_cnpj: dict[str, str] = {}
    for r in rows:
        cnpj = _so_digitos(r["cnpj_embarcador"])
        if not cnpj:
            continue
        nome = r["nome_remetente"] or r["apelido"] or ""
        if nome and cnpj not in nome_por_cnpj:
            nome_por_cnpj[cnpj] = nome

    mapa: dict[int, str] = {}
    for r in rows:
        sid = r["sender_id"]
        cnpj = _so_digitos(r["cnpj_embarcador"])
        if cnpj and cnpj in nome_por_cnpj:
            nome_final = nome_por_cnpj[cnpj]
        else:
            nome_final = r["nome_remetente"] or r["apelido"] or f"Sender #{sid}"
        mapa[sid] = _agrupar_por_prefixo(nome_final)
    return mapa


def _agrupar_por_prefixo(nome: str) -> str:
    """
    Agrupa unidades da mesma rede pelo prefixo antes do primeiro " - "
    (ex: "MARCHEF - ITAUEIRA" e "MARCHEF - GOURMAR" viram só
    "MARCHEF") — confirmado pelo Hugo, 30/07: são franquias/unidades
    da mesma rede, cada uma com CNPJ genuinamente diferente (não é bug
    de cadastro — confirmado com debug/investigar_embarcadores_
    duplicados.py, 42 CNPJs realmente distintos), mas ele quer ver
    como 1 embarcador só no dashboard. Nomes sem " - " não mudam.
    """
    if " - " in nome:
        prefixo = nome.split(" - ")[0].strip()
        return prefixo or nome
    return nome


def _nome_destinatario_do_titulo(titulo: str, customer_id) -> str:
    """
    Extrai um nome legível do destinatário a partir do título do
    serviço — não temos uma tabela local de nome por customer_id,
    então isso é best-effort só para exibição.
    """
    partes = (titulo or "").split(" - ")
    if len(partes) >= 2:
        return partes[-1].strip() or f"Cliente #{customer_id}"
    return f"Cliente #{customer_id}"


def _apelido_do_titulo(titulo: str) -> str | None:
    """
    Extrai o nome do embarcador do título do pedido, pra usar quando o
    sender_id NÃO está cadastrado em `interno` (confirmado em produção,
    30/07: 358 sender_id sem cadastro, 1524 pedidos — a maioria eram
    embarcadores reais só sem cadastro, não lixo; o maior sozinho tinha
    727 pedidos). Formatos observados:
      "#PS-XXXXX - REF/APELIDO - Destinatário"       (com código)
      "REF/APELIDO - Unidade - Destinatário"          (sem código)
    O resultado passa por _agrupar_por_prefixo() depois — então
    "MARCHEF - JEE" extraído aqui ainda vira só "MARCHEF" no final,
    juntando com as unidades que JÁ estavam cadastradas.

    Retorna None se o título não tiver o formato esperado (sem "/" em
    lugar nenhum) ou o resultado extraído parecer só um número —
    nesses casos o chamador cai de volta pro "Sender #XXXX" de sempre,
    sem piorar nada.
    """
    titulo = (titulo or "").strip()
    partes = titulo.split(" - ")
    if len(partes) < 2:
        return None

    # descarta só o ÚLTIMO segmento (destinatário) — o resto é
    # embarcador/unidade, com ou sem um código de pedido na frente
    bloco = " - ".join(partes[:-1])
    if partes[0].strip().upper().lstrip("#").startswith("PS-"):
        resto = bloco.split(" - ", 1)
        bloco = resto[1] if len(resto) > 1 else ""

    if not bloco or "/" not in bloco:
        return None

    _, _, restante = bloco.partition("/")
    resultado = restante.strip()
    if not resultado or resultado.replace(" ", "").isdigit():
        return None
    return resultado


# Override manual de agrupamento (pedido do Hugo, 30/07) — algumas
# empresas continuam com nomes diferentes mesmo depois do merge por
# CNPJ + agrupamento por prefixo + extração do título, porque são a
# MESMA empresa sob razões sociais/marcas/franquias diferentes —
# conhecimento de negócio que não dá pra inferir só do dado. Mantida
# manualmente; adicionar aqui sempre que o Hugo confirmar outro caso.
GRUPOS_MANUAIS = {
    # Grupo MARCHEF
    "MARCHEF": "MARCHEF",
    "TRES M EMPREENDIMENTOS LTDA": "MARCHEF",
    "MOROTA": "MARCHEF",
    "NÃO SEPARAR/MARCHEF": "MARCHEF",
    # Grupo DE TOMMASO
    "CIAO": "DE TOMMASO",
    "DE TOMMASO": "DE TOMMASO",
    # Grupo GRUPO TRIGO
    "GRUPO TRIGO": "GRUPO TRIGO",
    "GAZZINO": "GRUPO TRIGO",
    # Grupo OUTROS (casos sem empresa identificável de verdade)
    "Sender #16090864": "OUTROS",
    "CRIOMAR": "OUTROS",
    "CIA ZAFFARI COM E IND": "OUTROS",
    "RKWSP PATISSERIE LTDA": "OUTROS",
    "Sem embarcador": "OUTROS",
    "NÃO ENCONTRADO": "OUTROS",
    # Grupo JATOBÁ
    "JATOBÁ": "JATOBÁ",
    "LUCIANO AURELIO GAMBARINI ME": "JATOBÁ",
}


def _aplicar_grupo_manual(nome: str) -> str:
    """Aplica o override manual de agrupamento, se o nome estiver na tabela
    GRUPOS_MANUAIS. Nomes fora da tabela voltam sem alteração."""
    return GRUPOS_MANUAIS.get(nome, nome)


def enriquecer_registros(registros: list[dict]) -> list[dict]:
    """
    Enriquece os registros reduzidos com embarcador (já unificado por
    CNPJ, com fallback pro nome extraído do título quando o sender_id
    não está cadastrado), nome do destinatário, e ano/mês/semana
    derivados da data. Base pra todas as agregações (resumo e filtros
    interativos).
    """
    mapa_emb = _carregar_mapa_embarcadores()
    nome_destinatario: dict = {}
    nome_extra_por_sid: dict = {}
    enriquecidos = []

    for r in registros:
        data_str = r.get("data")
        if not data_str:
            continue
        try:
            d = datetime.strptime(data_str, "%Y-%m-%d").date()
        except ValueError:
            continue

        cid = r.get("customer_id")
        if cid and cid not in nome_destinatario:
            nome_destinatario[cid] = _nome_destinatario_do_titulo(r.get("title", ""), cid)

        sid = r.get("sender_id")
        if sid is None:
            embarcador = "Sem embarcador"
        elif sid in mapa_emb:
            embarcador = mapa_emb[sid]
        elif sid in nome_extra_por_sid:
            embarcador = nome_extra_por_sid[sid]
        else:
            extraido = _apelido_do_titulo(r.get("title", ""))
            embarcador = _agrupar_por_prefixo(extraido) if extraido else f"Sender #{sid}"
            nome_extra_por_sid[sid] = embarcador
        embarcador = _aplicar_grupo_manual(embarcador)

        enriquecidos.append({
            "data": data_str,
            "ano": d.year,
            "mes": f"{d.year:04d}-{d.month:02d}",
            "semana": _semana_iso(data_str),
            "embarcador": embarcador,
            "customer_id": cid,
            "nome_destinatario": nome_destinatario.get(cid) if cid else None,
        })

    return enriquecidos


def montar_dashboard(registros: list[dict]) -> tuple[dict, list[dict]]:
    """
    A partir da lista combinada de registros (ano inteiro), monta o
    resumo (cards, pedidos por semana, fluxo mensal por embarcador,
    top destinatários recorrentes) E a lista enriquecida (usada pelos
    filtros interativos). Retorna (resumo, enriquecidos).
    """
    enriquecidos = enriquecer_registros(registros)

    por_semana = Counter()
    por_destinatario = Counter()
    fluxo_embarcador: dict[str, Counter] = defaultdict(Counter)
    meses_vistos: set[str] = set()

    for r in enriquecidos:
        por_semana[r["semana"]] += 1
        meses_vistos.add(r["mes"])
        fluxo_embarcador[r["embarcador"]][r["mes"]] += 1
        if r["customer_id"]:
            por_destinatario[r["customer_id"]] += 1

    semanas_ordenadas = sorted(por_semana.keys())
    meses_ordenados = sorted(meses_vistos)

    fluxo_tabela = []
    for nome, contagem_mes in fluxo_embarcador.items():
        total = sum(contagem_mes.values())
        fluxo_tabela.append({
            "nome": nome,
            "meses": [contagem_mes.get(m, 0) for m in meses_ordenados],
            "total": total,
        })
    fluxo_tabela.sort(key=lambda e: e["total"], reverse=True)

    top_destinatarios = filtrar_destinatarios_recorrentes(enriquecidos)
    clientes_recorrentes = sum(1 for qtd in por_destinatario.values() if qtd > 1)

    resumo = {
        "atualizado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_pedidos": len(registros),
        "total_embarcadores": len(fluxo_embarcador),
        "total_destinatarios": len(por_destinatario),
        "clientes_recorrentes": clientes_recorrentes,
        "semanas": semanas_ordenadas,
        "pedidos_por_semana": [por_semana[s] for s in semanas_ordenadas],
        "meses": meses_ordenados,
        "fluxo_embarcadores": fluxo_tabela,
        "top_destinatarios": top_destinatarios,
    }
    return resumo, enriquecidos


# ── Funções de filtro (usadas pelas rotas /api/... do Flask) ───────────────

def filtrar_destinatarios_recorrentes(enriquecidos: list[dict], ano: int | None = None,
                                      mes: str | None = None, top_n: int = 20) -> list[dict]:
    """Top destinatários recorrentes, opcionalmente filtrado por ano e/ou mês."""
    contagem = Counter()
    nomes = {}
    for r in enriquecidos:
        if ano is not None and r["ano"] != ano:
            continue
        if mes is not None and r["mes"] != mes:
            continue
        cid = r.get("customer_id")
        if not cid:
            continue
        contagem[cid] += 1
        nomes.setdefault(cid, r.get("nome_destinatario") or f"Cliente #{cid}")
    return [{"nome": nomes[cid], "pedidos": qtd} for cid, qtd in contagem.most_common(top_n)]


def pedidos_semana_por_embarcador(enriquecidos: list[dict], embarcador: str) -> dict:
    """{"semanas": [...], "pedidos": [...]} pra UM embarcador específico."""
    contagem = Counter()
    for r in enriquecidos:
        if r["embarcador"] == embarcador:
            contagem[r["semana"]] += 1
    semanas = sorted(contagem.keys())
    return {"semanas": semanas, "pedidos": [contagem[s] for s in semanas]}


def pedidos_por_embarcador_periodo(enriquecidos: list[dict], ano: int | None = None,
                                   mes: str | None = None, semana: str | None = None) -> list[dict]:
    """
    Total de pedidos por embarcador, filtrado pelo período mais
    específico informado: semana (mais específico) > mês > ano > tudo.
    """
    contagem = Counter()
    for r in enriquecidos:
        if semana is not None:
            if r["semana"] != semana:
                continue
        elif mes is not None:
            if r["mes"] != mes:
                continue
        elif ano is not None:
            if r["ano"] != ano:
                continue
        contagem[r["embarcador"]] += 1
    return [{"nome": nome, "pedidos": qtd} for nome, qtd in contagem.most_common()]


def opcoes_filtro(enriquecidos: list[dict]) -> dict:
    """Valores distintos disponíveis pra popular os seletores de filtro."""
    anos = sorted({r["ano"] for r in enriquecidos})
    meses = sorted({r["mes"] for r in enriquecidos})
    semanas = sorted({r["semana"] for r in enriquecidos})
    embarcadores = sorted({r["embarcador"] for r in enriquecidos})
    return {"anos": anos, "meses": meses, "semanas": semanas, "embarcadores": embarcadores}
