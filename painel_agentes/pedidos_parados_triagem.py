# -*- coding: utf-8 -*-
"""
pedidos_parados_triagem.py

Tela de triagem dos "pedidos parados" do Fresh Hub
(freshhub.com.br/pedidos-parados) -- pedido do Hugo, 24/08: toda vez
que aparecerem esses pedidos, verificar se já foram tratados, duplicar
o que for necessário (Reenvio), e direcionar pra operação (Cancelados/
Devolução Parcial) os que tiverem necessidade -- sem duplicar nesses
casos.

Ver TRATATIVAS_PEDIDOS_PARADOS.md (raiz do projeto) pro contexto de
negócio completo -- classificações, regra de prioridade (>2 dias),
molde de cada Demanda.

A classificação em si é NOSSA (não existe campo de status em
`stalled_orders` no Fresh Hub, achado 24/08) -- fica na tabela local
`pedidos_parados_classificacao`, chaveada por `order_number` (não por
`freshhub_id`): o mesmo pedido pode ser registrado como parado mais de
uma vez no Fresh Hub (é re-registrado todo dia enquanto continua
parado, achado 24/08 -- teve caso de 35 dias seguidos), e a
classificação/tratativa precisa "grudar" no pedido, não em cada
registro individual -- é assim que "verificar se já foi tratado"
funciona de fato.

A LISTA exibida (`listar_com_classificacao`) só mostra quem foi
registrado de novo no dia mais recente (pedido do Hugo, 24/08) e já
tira quem foi entregue por fora da triagem (`completed_at` na Vuupt,
sem insucesso) -- mas `dias_parado` usa o histórico completo, não só o
registro de hoje.
"""
import html
import importlib.util
import logging
import re
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))

import yaml

import email_utils
import tratativas
from vuupt_client import VuuptClient, VuuptAPIError
from freshhub.auth import FreshHubSession
from freshhub.pedidos_parados import listar_pedidos_parados
from freshhub.tasks import criar_demanda_para_tratativa

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"

CLASSIFICACOES_VALIDAS = ("Cancelados", "Devolução Parcial", "Reenvio", "Agendado", "Descartar", "Verificar com Cliente", "Em Rota", "Cliente Retira", "Entregue")
CLASSIFICACOES_QUE_CRIAM_DEMANDA = ("Cancelados", "Devolução Parcial", "Descartar", "Verificar com Cliente")

DIAS_PRIORIDADE = 2  # pedido do Hugo, 24/08

FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")  # mesmo fuso da Torre de Controle

# Quantos registros brutos buscar no Fresh Hub pra ter histórico
# suficiente de "há quantos dias esse pedido aparece" -- o mesmo pedido
# é re-registrado todo dia enquanto continua parado (achado 24/08), então
# 200 (o default de listar_pedidos_parados) só cobre uns 3 dias de
# histórico; usamos uma janela maior aqui especificamente pra não
# subestimar `dias_parado` de pedidos parados há muito tempo.
LIMITE_HISTORICO_PEDIDOS_PARADOS = 2000

# Janela de busca de serviços concluídos na Vuupt pra saber quem já foi
# entregue (pedido do Hugo, 24/08) -- só precisa cobrir "recente" (o
# pedido só aparece na lista se ainda foi registrado como parado no dia
# mais recente; se tivesse sido entregue há muito tempo, não teria sido
# re-registrado). Insucesso (completed_at preenchido só que com
# failed_reason_id) NÃO conta como entregue -- continua na lista, já
# tem tratativa própria na Torre.
DIAS_JANELA_ENTREGUES = 3

# Link direto pro pedido na Stokki (mesma convenção de expedir_pedidos.py:
# URL_PROVIDER_SHW) e pra tela do pedido na Vuupt -- pedido do Hugo,
# 25/08. Confirmado pelo Hugo, 25/08: https://app.vuupt.com/manager/orders/{id}
# (o id numérico do próprio serviço, não o code "PS-XXXXX").
STOKKI_BASE = "https://freshlog.stokki.com.br"
URL_PEDIDO_STOKKI = f"{STOKKI_BASE}/pt-br/provider/inventory/outbound/show"
URL_SERVICO_VUUPT = "https://app.vuupt.com/manager/orders"

# Embarcadores cujo `order_number` do Fresh Hub é a NF do cliente, não o
# ID do Stokki -- o `code` do serviço na Vuupt é o PS interno, sem
# relação numérica com a NF (achado 25/08, caso Quatro Estrelas). A NF
# só aparece no `title` do serviço, sempre no formato "#PS-XXXXX - {NF}
# / {apelido} / {destinatário}" (apelido = coluna `nome_remetente` da
# tabela `interno`, é como pipeline.montar_payload_vuupt monta o
# título) -- com espaços em volta da barra. Confirmado via API real pra
# cada um, pedido do Hugo 25/08: Quatro Estrelas (pedido 175780,
# apelido "QUATRO ESTRELAS") e Laticínio Dourado (pedido 150282,
# apelido "DOURADO").
EMBARCADORES_ORDER_NUMBER_E_NF = ["QUATRO ESTRELAS", "DOURADO"]

# ── Cache da parte externa da listagem ─────────────────────────────────
# Por quê (incidente 25/08 à tarde): a listagem resolvia o link da Stokki
# pedido a pedido na Vuupt (busca de título, 2 chamadas por pedido -- uma
# por apelido em EMBARCADORES_ORDER_NUMBER_E_NF) pra TODO pedido que não
# casava em `pedidos_historico` -- que na prática eram todos (55 de 55
# naquele dia = 110 buscas por carga), e a tela recarrega a cada 60s POR
# ABA. Estourou o rate limit da Vuupt (HTTP 429 persistindo além dos 5
# retries do http_retry) e a tela ficou em "Falha ao carregar" a tarde
# inteira, derrubando de quebra as outras integrações com a Vuupt no
# mesmo minuto.
#
# Três defesas, todas aqui:
# 1. A base externa (Fresh Hub + entregues + ids Stokki) é montada uma
#    vez a cada TTL_BASE_EXTERNA_SEG, sob lock (single-flight): N abas
#    abertas = 1 montagem. A classificação local é lida fresca sempre,
#    então uma ação do operador aparece na hora, sem esperar o cache.
# 2. O mapeamento order_number -> id Stokki achado na Vuupt é PERSISTIDO
#    em SQLite (não muda nunca: o PS de uma NF é fixo) -- sobrevive ao
#    restart do painel, que acontece a cada deploy. Quem NÃO foi achado
#    fica num cache negativo em memória por TTL_NEGATIVO_ID_STOKKI_SEG
#    (não é dos embarcadores de NF, ou o serviço ainda não existe).
# 3. Por carga, no máximo MAX_BUSCAS_VUUPT_POR_CARGA pedidos novos vão à
#    Vuupt -- os demais ficam pra próxima carga (o link cai no default,
#    o próprio order_number, até resolver). E se a Vuupt falhar no meio
#    (429 esgotado, rede), a carga NÃO derruba: loga, para de tentar
#    nessa rodada e devolve o que tem -- o link da Stokki é o único
#    afetado, e só pros pedidos dos 2 embarcadores de NF.
TTL_BASE_EXTERNA_SEG = 60
TTL_ENTREGUES_SEG = 300            # lista paginada de concluídos dos últimos 3 dias, muda devagar
TTL_NEGATIVO_ID_STOKKI_SEG = 6 * 3600
MAX_BUSCAS_VUUPT_POR_CARGA = 10    # pedidos (cada um = até 2 chamadas)

_lock_cache = threading.Lock()
_cache_base: dict = {"quando": 0.0, "dados": None}
_cache_entregues: dict = {"quando": 0.0, "dados": None}
_ids_stokki_nao_achados: dict[str, float] = {}  # order_number -> monotonic da última tentativa sem sucesso


def _buscar_servico_por_nf_embarcadores(vuupt: VuuptClient, order_number: str) -> dict | None:
    """Tenta achar o serviço na Vuupt pelo título, pros embarcadores em
    EMBARCADORES_ORDER_NUMBER_E_NF (ver comentário acima) -- usado tanto
    por _resolver_pedido (ações sob demanda) quanto por
    _resolver_ids_stokki (link da Stokki na listagem)."""
    for apelido in EMBARCADORES_ORDER_NUMBER_E_NF:
        servico = vuupt.buscar_servico_por_titulo_contendo(f"{order_number} / {apelido}")
        if servico:
            return servico
    return None


_modulo_expedir_pedidos = None  # cache do import explícito, ver _expedir_pedidos_raiz()


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _conectar():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_parados_classificacao (
            order_number     TEXT PRIMARY KEY,
            freshhub_id      TEXT,
            classificacao    TEXT,
            classificado_por TEXT,
            classificado_em  TEXT,
            acao_status      TEXT,
            acao_detalhe     TEXT,
            acao_em          TEXT
        )
    """)
    # order_number (Fresh Hub) -> id numérico real do Stokki, achado na
    # Vuupt pelo título (ver _resolver_ids_stokki). Persistido porque não
    # muda e porque reconstruir custa chamadas na Vuupt (incidente 25/08).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_parados_id_stokki (
            order_number TEXT PRIMARY KEY,
            id_stokki    TEXT NOT NULL,
            resolvido_em TEXT
        )
    """)
    # Último resultado da consulta em lote à Stokki (botão "Consultar
    # Stokki", pedido do Hugo 28/08) -- guardado pra tela mostrar o status/
    # transportadora vistos, sem reconsultar a cada recarga.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_parados_stokki (
            order_number   TEXT PRIMARY KEY,
            id_stokki      TEXT,
            status         TEXT,
            transportadora TEXT,
            consultado_em  TEXT
        )
    """)
    # Último resultado da consulta em lote à Vuupt (botão "Consultar
    # Vuupt", pedido do Hugo 28/08) -- mesmo papel da tabela acima.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pedidos_parados_vuupt (
            order_number    TEXT PRIMARY KEY,
            code            TEXT,
            status          TEXT,
            scheduled_start TEXT,
            consultado_em   TEXT
        )
    """)
    conn.commit()
    return conn


def _sessao_freshhub() -> FreshHubSession:
    return FreshHubSession(_carregar_config())


def _vuupt() -> VuuptClient:
    token = _carregar_config().get("vuupt_api", {}).get("token", "")
    return VuuptClient(token)


def _expedir_pedidos_raiz():
    """Import explícito do expedir_pedidos.py da RAIZ -- mesmo motivo de
    torre_controle.py: `import expedir_pedidos` simples resolveria pra
    cópia dentro de insucesso_entrega/ por causa da ordem do sys.path
    (armadilha já vivida em produção com esse mesmo par de arquivos)."""
    global _modulo_expedir_pedidos
    if _modulo_expedir_pedidos is None:
        caminho = _RAIZ / "expedir_pedidos.py"
        spec = importlib.util.spec_from_file_location("expedir_pedidos_raiz_ppt", caminho)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        _modulo_expedir_pedidos = modulo
    return _modulo_expedir_pedidos


def _resolver_pedido(vuupt: VuuptClient, order_number: str) -> tuple[dict | None, str | None]:
    """
    Acha o serviço na Vuupt a partir do `order_number` do Fresh Hub --
    que pode ser o ID numérico do Stokki (a parte de PS-XXXXX) OU a NF
    do cliente, sem jeito de saber qual só olhando o valor (achado
    24/08, ver TRATATIVAS_PEDIDOS_PARADOS.md). Tenta os dois, nessa
    ordem, e só então o caso dos embarcadores em
    EMBARCADORES_ORDER_NUMBER_E_NF (abaixo).

    Retorna (servico, pedido_code) -- pedido_code no formato "PS-XXXXX"
    (mesmo padrão usado por tratativas.py) -- ou (None, None) se não
    achar de nenhum jeito.
    """
    code_stokki = f"PS-{order_number}"
    servico = vuupt.buscar_servico_por_code(code_stokki)
    if servico:
        return servico, code_stokki

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id_pedido FROM pedidos_historico WHERE numero_nfe = ?",
            (order_number,),
        ).fetchone()
    finally:
        conn.close()

    if row and row["id_pedido"]:
        code = row["id_pedido"].lstrip("#")
        servico = vuupt.buscar_servico_por_code(code)
        if servico:
            return servico, code

    # Embarcadores em EMBARCADORES_ORDER_NUMBER_E_NF (Quatro Estrelas,
    # Laticínio Dourado): o order_number do Fresh Hub é a NF do cliente,
    # mas o `code` do serviço na Vuupt é o PS interno do Stokki -- sem
    # relação numérica com a NF (achado 25/08, pedido do Hugo) -- então
    # as duas tentativas acima nunca acham nada pra esses embarcadores. A
    # NF SÓ aparece no `title` do serviço (ver comentário de
    # EMBARCADORES_ORDER_NUMBER_E_NF pro formato exato e a confirmação
    # via API real de cada um).
    servico = _buscar_servico_por_nf_embarcadores(vuupt, order_number)
    if servico and servico.get("code"):
        return servico, servico["code"].lstrip("#")

    return None, None


def _resolver_ids_stokki(vuupt: VuuptClient, order_numbers: list[str]) -> dict[str, str]:
    """
    ID numérico real do Stokki (o final de /outbound/show/{id}) pra cada
    order_number -- mesma ambiguidade de _resolver_pedido: o
    order_number do Fresh Hub pode já SER o ID Stokki (caso mais comum)
    ou pode ser a NF do cliente, que só mapeia pro ID Stokki via
    `pedidos_historico` (ver TRATATIVAS_PEDIDOS_PARADOS.md). Prioriza o
    banco local (rápido, cobre a maioria da lista); só recorre à Vuupt
    (busca de título, ver EMBARCADORES_ORDER_NUMBER_E_NF) pros que
    sobraram sem resolver -- pedido do Hugo, 25/08: sem esse fallback o
    link da Stokki saía errado (usava a NF como se fosse o ID do
    Stokki) pra Quatro Estrelas e Laticínio Dourado, os únicos
    embarcadores nesse caso hoje.

    Default: o próprio order_number, quando não acha nada de nenhum
    jeito (é o caso mais comum -- já É o ID Stokki).

    A ida à Vuupt é a parte cara e foi o que derrubou a tela em 25/08
    (ver comentário de TTL_BASE_EXTERNA_SEG): o que ela acha é gravado em
    `pedidos_parados_id_stokki` pra nunca mais perguntar; o que ela NÃO
    acha entra num cache negativo em memória; e por chamada no máximo
    MAX_BUSCAS_VUUPT_POR_CARGA pedidos novos são consultados -- o resto
    fica pra próxima carga. Qualquer falha na Vuupt encerra a rodada
    (não derruba a listagem) e não é cacheada, pra tentar de novo depois.
    """
    if not order_numbers:
        return {}

    resultado = {n: n for n in order_numbers}
    marcadores = ",".join("?" * len(order_numbers))
    conn = _conectar()
    try:
        linhas = conn.execute(
            f"SELECT numero_nfe, id_pedido FROM pedidos_historico WHERE numero_nfe IN ({marcadores})",
            list(order_numbers),
        ).fetchall()
        persistidos = conn.execute(
            f"SELECT order_number, id_stokki FROM pedidos_parados_id_stokki WHERE order_number IN ({marcadores})",
            list(order_numbers),
        ).fetchall()
    finally:
        conn.close()

    resolvidos = set()
    for linha in linhas:
        m = re.search(r"PS-(\d+)", linha["id_pedido"] or "")
        if m:
            resultado[linha["numero_nfe"]] = m.group(1)
            resolvidos.add(linha["numero_nfe"])
    for linha in persistidos:
        resultado[linha["order_number"]] = linha["id_stokki"]
        resolvidos.add(linha["order_number"])

    agora = time.monotonic()
    pendentes = [
        n for n in order_numbers
        if n not in resolvidos
        and agora - _ids_stokki_nao_achados.get(n, -float("inf")) >= TTL_NEGATIVO_ID_STOKKI_SEG
    ]
    if not pendentes:
        return resultado
    if len(pendentes) > MAX_BUSCAS_VUUPT_POR_CARGA:
        logger.info(
            f"[pedidos-parados] {len(pendentes)} pedido(s) sem id Stokki resolvido; consultando "
            f"{MAX_BUSCAS_VUUPT_POR_CARGA} na Vuupt nesta carga, o resto fica pra próxima."
        )
        pendentes = pendentes[:MAX_BUSCAS_VUUPT_POR_CARGA]

    achados: dict[str, str] = {}
    for numero in pendentes:
        try:
            servico = _buscar_servico_por_nf_embarcadores(vuupt, numero)
        except Exception as e:
            logger.warning(
                f"[pedidos-parados] Vuupt falhou ao resolver id Stokki do pedido {numero} "
                f"(parando nesta carga, tento de novo na próxima): {e}"
            )
            break
        m = re.search(r"PS-(\d+)", (servico or {}).get("code") or "")
        if m:
            achados[numero] = m.group(1)
            resultado[numero] = m.group(1)
        else:
            _ids_stokki_nao_achados[numero] = agora

    if achados:
        carimbo = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = _conectar()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO pedidos_parados_id_stokki (order_number, id_stokki, resolvido_em) VALUES (?, ?, ?)",
                [(n, i, carimbo) for n, i in achados.items()],
            )
            conn.commit()
        finally:
            conn.close()

    return resultado


def _resolver_embarcador(order_number: str) -> dict | None:
    """
    Acha o embarcador (nome + e-mails cadastrados) do pedido pra
    notificação de "Cliente Retira" (pedido do Hugo, 25/08) -- cruza
    `pedidos_historico` (mesma ambiguidade de order_number = ID Stokki
    OU NF do cliente que _resolver_pedido trata pra Vuupt) com
    `interno` pelo CNPJ, não pelo nome (mais confiável que fuzzy match):
    `pedidos_historico.cliente_cnpj` vem formatado (com pontuação),
    `interno.cnpj_embarcador` vem só dígitos -- comparamos só os
    dígitos dos dois lados.

    Retorna None se o pedido não estiver em `pedidos_historico` (ainda
    não passou pelo pipeline), se o embarcador não estiver cadastrado em
    `interno`, se `notificar_email` estiver desligado pra ele, ou se não
    houver e-mail válido cadastrado -- em qualquer um desses casos quem
    chama decide como avisar (não dá pra mandar e-mail sem destinatário).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id_pedido, cliente, cliente_cnpj FROM pedidos_historico "
            "WHERE id_pedido = ? OR id_pedido = ? OR numero_nfe = ?",
            (f"#PS-{order_number}", f"PS-{order_number}", order_number),
        ).fetchone()
        if not row or not row["cliente_cnpj"]:
            return None

        digitos = re.sub(r"\D", "", row["cliente_cnpj"])
        if not digitos:
            return None

        emb = conn.execute(
            "SELECT nome_remetente, apelido, email, notificar_email FROM interno "
            "WHERE cnpj_embarcador = ?",
            (digitos,),
        ).fetchone()
    finally:
        conn.close()

    if not emb:
        return None
    if emb["notificar_email"] is not None and not emb["notificar_email"]:
        return None

    emails = [e.strip() for e in re.split(r"[,;\t]+", emb["email"] or "") if e.strip() and "@" in e]
    if not emails:
        return None

    return {
        "codigo": row["id_pedido"] or f"PS-{order_number}",
        "nome": emb["apelido"] or emb["nome_remetente"] or row["cliente"] or "",
        "emails": emails,
    }


def _marcar_acao(order_number: str, status: str, detalhe: str) -> None:
    conn = _conectar()
    conn.execute("""
        UPDATE pedidos_parados_classificacao
        SET acao_status = ?, acao_detalhe = ?, acao_em = ?
        WHERE order_number = ?
    """, (status, detalhe, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), order_number))
    conn.commit()
    conn.close()


def _data_local(iso_utc: str) -> date:
    """Converte um created_at do Fresh Hub (ISO em UTC) pra data local
    (America/Sao_Paulo) -- usado pra saber em que DIA o registro
    aconteceu de verdade (perto da meia-noite, UTC e local podem cair
    em dias diferentes)."""
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(FUSO_LOCAL).date()


def _codes_entregues_recentes(vuupt: VuuptClient, dias: int = DIAS_JANELA_ENTREGUES) -> set[str]:
    """
    Códigos (sem '#') de serviços concluídos com SUCESSO (completed_at
    preenchido e sem failed_reason_id) nos últimos `dias` dias -- busca
    em lote (1 chamada paginada) em vez de resolver pedido a pedido, bem
    mais rápido (pedido do Hugo, 24/08: pedidos entregues somem da
    lista). Insucesso (completed_at preenchido só que com
    failed_reason_id) fica de fora do set de propósito -- já tem
    tratativa própria na Torre de Controle, não é "resolvido" pra fins
    de pedido parado.

    Lança a exceção da Vuupt pra quem chama (_entregues_com_cache decide
    entre reaproveitar o último resultado bom e não filtrar nada).
    """
    inicio = date.today() - timedelta(days=dias)
    filtros = [{"field": "completed_at", "operator": "gte", "value": inicio.strftime("%Y-%m-%d")}]
    servicos = vuupt.listar_servicos(filtros)
    return {
        s["code"].lstrip("#")
        for s in servicos
        if s.get("code") and s.get("completed_at") and not s.get("failed_reason_id")
    }


def _entregues_com_cache(vuupt: VuuptClient) -> set[str]:
    """Set de _codes_entregues_recentes, renovado a cada TTL_ENTREGUES_SEG.
    Se a Vuupt falhar, reaproveita o último resultado bom (mesmo vencido)
    ou, sem nenhum, não filtra nada -- em vez de derrubar a listagem.
    Chamado só de dentro de _base_externa_com_cache (já sob _lock_cache)."""
    idade = time.monotonic() - _cache_entregues["quando"]
    if _cache_entregues["dados"] is not None and idade < TTL_ENTREGUES_SEG:
        return _cache_entregues["dados"]
    try:
        dados = _codes_entregues_recentes(vuupt)
    except Exception as e:
        if _cache_entregues["dados"] is not None:
            logger.warning(f"Falha ao buscar serviços concluídos recentes (usando último resultado bom): {e}")
            return _cache_entregues["dados"]
        logger.warning(f"Falha ao buscar serviços concluídos recentes (não filtro entregues por segurança): {e}")
        return set()
    _cache_entregues["quando"] = time.monotonic()
    _cache_entregues["dados"] = dados
    return dados


def _montar_base_externa() -> dict:
    """Tudo da listagem que vem de fora (Fresh Hub + Vuupt) -- separado
    da classificação local de propósito, pra poder cachear só isto."""
    sessao = _sessao_freshhub()
    brutos = listar_pedidos_parados(sessao, limit=LIMITE_HISTORICO_PEDIDOS_PARADOS)
    if not brutos:
        return {"pedidos_do_dia": {}, "primeira_vez": {}, "contagem": {},
                "dia_mais_recente": None, "ids_stokki": {}}

    primeira_vez: dict[str, str] = {}
    ultima_vez: dict[str, dict] = {}
    contagem: dict[str, int] = {}
    for p in brutos:
        numero = p["order_number"]
        contagem[numero] = contagem.get(numero, 0) + 1
        if numero not in primeira_vez or p["created_at"] < primeira_vez[numero]:
            primeira_vez[numero] = p["created_at"]
        atual = ultima_vez.get(numero)
        if atual is None or p["created_at"] > atual["created_at"]:
            ultima_vez[numero] = p

    dia_mais_recente = max(_data_local(p["created_at"]) for p in brutos)
    pedidos_do_dia = {
        numero: p for numero, p in ultima_vez.items()
        if _data_local(p["created_at"]) == dia_mais_recente
    }

    vuupt = _vuupt()
    entregues = _entregues_com_cache(vuupt)
    pedidos_do_dia = {
        numero: p for numero, p in pedidos_do_dia.items()
        if f"PS-{numero}" not in entregues
    }
    ids_stokki = _resolver_ids_stokki(vuupt, list(pedidos_do_dia.keys()))

    return {"pedidos_do_dia": pedidos_do_dia, "primeira_vez": primeira_vez, "contagem": contagem,
            "dia_mais_recente": dia_mais_recente, "ids_stokki": ids_stokki}


def _base_externa_com_cache() -> dict:
    """_montar_base_externa a cada TTL_BASE_EXTERNA_SEG, sob lock: várias
    abas recarregando ao mesmo tempo (a tela faz isso a cada 60s cada)
    viram UMA montagem, as outras esperam e pegam o resultado pronto.
    Ver comentário de TTL_BASE_EXTERNA_SEG pro incidente que motivou."""
    with _lock_cache:
        idade = time.monotonic() - _cache_base["quando"]
        if _cache_base["dados"] is not None and idade < TTL_BASE_EXTERNA_SEG:
            return _cache_base["dados"]
        dados = _montar_base_externa()
        _cache_base["quando"] = time.monotonic()
        _cache_base["dados"] = dados
        return dados


# ── Leitura ────────────────────────────────────────────────────────────────────

def listar_com_classificacao() -> list[dict]:
    """
    Une os pedidos parados do Fresh Hub com a classificação local.

    Regras combinadas com o Hugo, 24/08:
    - Só mostra pedidos registrados no dia mais recente presente nos
      dados (o mesmo pedido é re-registrado todo dia enquanto continua
      parado -- se não foi re-registrado hoje, ou já foi resolvido, sai
      da lista de qualquer forma).
    - `dias_parado` olha o HISTÓRICO completo disponível (não só o
      registro de hoje) -- é a diferença entre hoje e a primeira vez
      que esse pedido apareceu como parado, senão a prioridade (>=2
      dias) nunca dispararia.
    - Pedidos já entregues (completed_at na Vuupt, sem insucesso) saem
      da lista mesmo que tenham sido re-registrados hoje.

    A parte externa (Fresh Hub + Vuupt) vem cacheada de
    _base_externa_com_cache; a classificação local é lida fresca a cada
    chamada, então uma ação do operador aparece na hora.
    """
    base = _base_externa_com_cache()
    pedidos_do_dia = base["pedidos_do_dia"]
    if not pedidos_do_dia:
        return []
    primeira_vez = base["primeira_vez"]
    contagem = base["contagem"]
    dia_mais_recente = base["dia_mais_recente"]
    ids_stokki = base["ids_stokki"]

    conn = _conectar()
    try:
        locais = {r["order_number"]: dict(r) for r in conn.execute(
            "SELECT * FROM pedidos_parados_classificacao"
        ).fetchall()}
        stokki = {r["order_number"]: dict(r) for r in conn.execute(
            "SELECT * FROM pedidos_parados_stokki"
        ).fetchall()}
        vuupt_visto = {r["order_number"]: dict(r) for r in conn.execute(
            "SELECT * FROM pedidos_parados_vuupt"
        ).fetchall()}
    finally:
        conn.close()

    resultado = []
    for numero, p in pedidos_do_dia.items():
        dias_parado = (dia_mais_recente - _data_local(primeira_vez[numero])).days + 1
        local = locais.get(numero, {})
        stk = stokki.get(numero, {})
        vu = vuupt_visto.get(numero, {})
        resultado.append({
            "vuupt_code": vu.get("code"),
            "vuupt_status": vu.get("status"),
            "vuupt_scheduled_start": vu.get("scheduled_start"),
            "vuupt_consultado_em": vu.get("consultado_em"),
            "stokki_status": stk.get("status"),
            "stokki_transportadora": stk.get("transportadora"),
            "stokki_consultado_em": stk.get("consultado_em"),
            "order_number": numero,
            "freshhub_id": p["id"],
            "volumes": p["volumes"],
            "recebedor_name": p["recebedor_name"],
            "created_at": p["created_at"],
            "vezes_registrado": contagem[numero],
            "stokki_url": f"{URL_PEDIDO_STOKKI}/{ids_stokki.get(numero, numero)}",
            "dias_parado": dias_parado,
            "prioridade": dias_parado >= DIAS_PRIORIDADE,
            "classificacao": local.get("classificacao"),
            "classificado_por": local.get("classificado_por"),
            "classificado_em": local.get("classificado_em"),
            "acao_status": local.get("acao_status"),
            "acao_detalhe": local.get("acao_detalhe"),
        })

    # mais dias parado primeiro -- são os que mais precisam de atenção
    resultado.sort(key=lambda r: r["dias_parado"], reverse=True)
    return resultado


# ── Ações ──────────────────────────────────────────────────────────────────────

def classificar(order_number: str, freshhub_id: str, classificacao: str, usuario: str) -> None:
    if classificacao not in CLASSIFICACOES_VALIDAS:
        raise ValueError(
            f"Classificação inválida: {classificacao!r} "
            f"(válidas: {', '.join(CLASSIFICACOES_VALIDAS)})"
        )
    agora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _conectar()
    conn.execute("""
        INSERT INTO pedidos_parados_classificacao
            (order_number, freshhub_id, classificacao, classificado_por, classificado_em, acao_status)
        VALUES (?, ?, ?, ?, ?, 'pendente')
        ON CONFLICT(order_number) DO UPDATE SET
            freshhub_id = excluded.freshhub_id,
            classificacao = excluded.classificacao,
            classificado_por = excluded.classificado_por,
            classificado_em = excluded.classificado_em,
            acao_status = 'pendente'
    """, (order_number, freshhub_id, classificacao, usuario, agora))
    conn.commit()
    conn.close()

    tratativas.registrar_evento(
        order_number, "PEDIDOS_PARADOS", "PEDIDO_PARADO_CLASSIFICADO",
        decisao=classificacao, texto=f"Classificado como {classificacao} por {usuario}",
    )


def duplicar(order_number: str, usuario: str) -> dict:
    """
    Ação da tratativa "Reenvio": resolve o pedido na Vuupt e duplica --
    mesma função usada no botão "Duplicar pedido" da Torre
    (expedir_pedidos.duplicar_servico_por_insucesso). Idempotente: não
    duplica de novo se esse service_id já foi duplicado antes (mesmo
    fingerprint usado pela Torre)."""
    import fingerprint_duplicacao_insucesso  # sys.path já tem insucesso_entrega/

    vuupt = _vuupt()
    servico, pedido_code = _resolver_pedido(vuupt, order_number)
    if not servico:
        raise ValueError(
            f"Não encontrei o pedido {order_number} na Vuupt (tentei como "
            f"ID do Stokki e como NF em pedidos_historico)."
        )

    service_id = servico["id"]
    if fingerprint_duplicacao_insucesso.ja_duplicado(service_id):
        novo_code = fingerprint_duplicacao_insucesso.buscar_novo_code(service_id)
        _marcar_acao(order_number, "concluida", f"já duplicado antes → {novo_code}")
        return {"ok": True, "novo_code": novo_code, "ja_existia": True}

    modulo = _expedir_pedidos_raiz()
    novo = modulo.duplicar_servico_por_insucesso(vuupt, servico)
    if not novo:
        _marcar_acao(order_number, "erro", "falha ao criar reentrega na Vuupt")
        raise RuntimeError("Falha ao criar a reentrega na Vuupt (ver log de expedição).")

    novo_code = novo.get("code", "")
    fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, novo_code)
    tratativas.registrar_evento(
        pedido_code, "PEDIDOS_PARADOS", "REENVIO_MANUAL",
        service_id=service_id,
        texto=f"Duplicado a partir da triagem de pedidos parados por {usuario} → {novo_code}",
    )
    _marcar_acao(order_number, "concluida", f"duplicado → {novo_code}")
    return {"ok": True, "novo_code": novo_code, "ja_existia": False}


def encaminhar_operacao(order_number: str, classificacao: str, usuario: str) -> dict:
    """
    Ação das tratativas que criam Demanda no Fresh Hub (Cancelados/
    Devolução Parcial/Descartar/Verificar com Cliente -- cada uma numa
    área diferente, ver MOLDE_TRATATIVA em freshhub/tasks.py; nunca
    duplica na Vuupt). Resolve o nome do cliente na Vuupt só pra usar no
    título/client_name do card -- se não conseguir, cria a Demanda com
    um título genérico em vez de travar a ação (é mais importante quem
    for tratar ver o card do que travar por causa de um nome que não
    resolveu)."""
    if classificacao not in CLASSIFICACOES_QUE_CRIAM_DEMANDA:
        raise ValueError(f"Classificação {classificacao!r} não cria Demanda.")

    vuupt = _vuupt()
    servico, pedido_code = _resolver_pedido(vuupt, order_number)
    client_name = f"Pedido {order_number}"
    if servico:
        customer = vuupt.buscar_customer_por_id(servico.get("customer_id"))
        if customer and customer.get("name"):
            client_name = customer["name"]
        else:
            logger.warning(
                f"Não resolvi o nome do cliente do pedido {order_number} -- "
                f"criando a Demanda com título genérico."
            )
    else:
        logger.warning(
            f"Não encontrei o pedido {order_number} na Vuupt -- criando a "
            f"Demanda com título genérico mesmo assim."
        )

    sessao_fh = _sessao_freshhub()
    criada = criar_demanda_para_tratativa(sessao_fh, classificacao, client_name)

    if pedido_code:
        tratativas.registrar_evento(
            pedido_code, "PEDIDOS_PARADOS", "PEDIDO_PARADO_ENCAMINHADO_OPERACAO",
            decisao=classificacao,
            texto=f"Demanda criada no Fresh Hub por {usuario} (id={criada.get('id')}, cliente={client_name})",
        )
    _marcar_acao(order_number, "concluida", f"demanda criada → {criada.get('id')}")
    return {"ok": True, "demanda_id": criada.get("id"), "client_name": client_name}


def notificar_cliente_retira(order_number: str, usuario: str) -> dict:
    """
    Ação da tratativa "Cliente Retira" (pedido do Hugo, 25/08): manda
    e-mail pro embarcador avisando que o pedido está aguardando retirada
    e ainda não foi coletado -- é a tratativa inteira (sem duplicar, sem
    Demanda, confirmado com o Hugo). Usa o mesmo template visual
    (`email_utils.envelope_html`) já usado pelos outros notificadores
    automáticos do projeto ("design que já temos desenvolvido").
    """
    embarcador = _resolver_embarcador(order_number)
    if not embarcador:
        detalhe = "embarcador não resolvido (sem CNPJ em pedidos_historico ou sem e-mail cadastrado em 'interno')"
        _marcar_acao(order_number, "erro", detalhe)
        raise ValueError(f"Pedido {order_number}: {detalhe}.")

    codigo = embarcador["codigo"]
    nome   = embarcador["nome"]
    emails = embarcador["emails"]

    conteudo = f"""
    <h2 style="margin:0 0 4px;font-size:20px;color:{email_utils.COR_TEXTO};">Pedido aguardando retirada</h2>
    <p style="margin:0 0 24px;font-size:14px;color:{email_utils.COR_TEXTO_SUAVE};">
      Olá, <strong>{html.escape(nome)}</strong>!<br><br>
      O pedido <strong>{html.escape(codigo)}</strong> está marcado como
      <strong>retirada pelo cliente</strong> e ainda não foi coletado.
      Por favor, oriente o cliente a providenciar a retirada o quanto antes
      para que possamos dar continuidade à expedição.
    </p>
    <p style="margin:0;font-size:14px;color:{email_utils.COR_TEXTO};line-height:1.6;">
      Atenciosamente,<br><strong>Freshlog Logística</strong>
    </p>"""
    corpo = email_utils.envelope_html(
        conteudo,
        rodape="Freshlog Logística -- aviso automático de pedido aguardando retirada.",
        cor_acento=email_utils.COR_DESTAQUE,
    )
    assunto = f"[Freshlog] Pedido {codigo} aguardando retirada"

    config_email = _carregar_config().get("email", {})
    if not email_utils.enviar_email(emails, assunto, corpo, config_email):
        detalhe = f"falha ao enviar e-mail pra {', '.join(emails)}"
        _marcar_acao(order_number, "erro", detalhe)
        raise RuntimeError(detalhe)

    pedido_code = codigo.lstrip("#")
    tratativas.registrar_evento(
        pedido_code, "PEDIDOS_PARADOS", "PEDIDO_PARADO_CLIENTE_RETIRA_NOTIFICADO",
        texto=f"E-mail de 'ainda não coletado' enviado por {usuario} pra {nome} ({', '.join(emails)})",
    )

    # Caso #PS-37190 (27/08): pedido tinha sido importado no VUUPT ANTES
    # de virar "Cliente Retira" no Stokki -- o filtro de RETIRADA em
    # pipeline.py só roda na importação, então o serviço ficou órfão no
    # VUUPT depois da reclassificação. Como esta ação já É a confirmação
    # de que o pedido é retirada pelo cliente, aproveita pra cancelar o
    # serviço aqui também, se ele existir e ainda não tiver sido tocado
    # (nunca cancela algo já atribuído/em rota/concluído -- nesse caso
    # só avisa, intervenção manual).
    detalhe_vuupt = ""
    try:
        servico = _vuupt().buscar_servico_por_code(codigo)
        if servico:
            status = servico.get("status")
            if status == "not_assigned":
                _vuupt().cancelar_servico(servico["id"])
                tratativas.registrar_evento(
                    pedido_code, "PEDIDOS_PARADOS", "SERVICO_VUUPT_CANCELADO_CLIENTE_RETIRA",
                    service_id=servico["id"],
                    texto=f"Serviço {servico['id']} cancelado no VUUPT por {usuario} -- pedido é retirada pelo cliente.",
                )
                detalhe_vuupt = f" (serviço {servico['id']} cancelado no VUUPT)"
            else:
                logger.warning(
                    f"{pedido_code}: é Cliente Retira, mas o serviço {servico['id']} no VUUPT "
                    f"está em status '{status}' (não 'not_assigned') -- não cancelado automaticamente, "
                    f"intervenção manual necessária."
                )
                detalhe_vuupt = f" (AVISO: serviço {servico['id']} no VUUPT em status '{status}', cancelar manualmente)"
    except VuuptAPIError as e:
        logger.warning(f"{pedido_code}: falha ao cancelar serviço no VUUPT -- {e}")
        detalhe_vuupt = f" (falha ao cancelar no VUUPT: {e})"

    _marcar_acao(order_number, "concluida", f"e-mail enviado → {nome} ({', '.join(emails)})" + detalhe_vuupt)
    return {"ok": True, "nome_embarcador": nome, "emails": emails, "detalhe_vuupt": detalhe_vuupt}


def _servico_com_sucesso(servico: dict) -> bool:
    """Mesmo critério de sucesso usado em _codes_entregues_recentes:
    completed_at preenchido e sem failed_reason_id."""
    return bool(servico.get("completed_at")) and not servico.get("failed_reason_id")


def buscar_sucesso_vuupt(order_number: str) -> dict:
    """
    Botão "Buscar na Vuupt" da triagem (pedido do Hugo, 25/08; ajustado
    25/08 pra andar a cadeia inteira de reentregas): procura o pedido
    original e, se ele já foi duplicado por insucesso (mesmo fingerprint
    que duplicar() usa pra Reenvio), segue a cadeia de reentregas até o
    fim -- uma reentrega pode falhar de novo e gerar outra reentrega
    (R1 -> R2 -> ...). Entre todos os candidatos da cadeia, devolve o
    de created_at mais recente que tenha sido entregue com SUCESSO
    (mesmo critério de _servico_com_sucesso), com link direto pra tela
    de serviços na Vuupt. Sob demanda (1 chamada por clique) em vez de
    embutido na listagem -- resolver na Vuupt é lento pra rodar pra
    cada linha da tabela toda vez que ela recarrega.
    """
    import fingerprint_duplicacao_insucesso  # sys.path já tem insucesso_entrega/

    vuupt = _vuupt()
    servico, _pedido_code = _resolver_pedido(vuupt, order_number)

    candidatos = []
    if servico:
        candidatos.append(servico)
        atual = servico
        while fingerprint_duplicacao_insucesso.ja_duplicado(atual["id"]):
            novo_code = fingerprint_duplicacao_insucesso.buscar_novo_code(atual["id"])
            if not novo_code:
                break
            reentrega = vuupt.buscar_servico_por_code(novo_code)
            if not reentrega:
                break
            candidatos.append(reentrega)
            atual = reentrega

    com_sucesso = [c for c in candidatos if _servico_com_sucesso(c)]
    if com_sucesso:
        mais_recente = max(com_sucesso, key=lambda s: s.get("created_at", ""))
        return {
            "ok": True,
            "encontrado": True,
            "code": mais_recente.get("code"),
            "link": f"{URL_SERVICO_VUUPT}/{mais_recente['id']}",
        }

    return {"ok": True, "encontrado": False}


# ── Consulta em lote à Stokki (status + transportadora) ───────────────────────
#
# Botão "Consultar Stokki" da triagem (pedido do Hugo, 28/08): pra todos os
# pedidos em tela, olha na Stokki o status e a transportadora e já
# classifica sozinho os casos óbvios -- "Cancelado" na Stokki vira
# "Cancelados"; transportadora de RETIRADA (CLIENTE RETIRA) vira "Cliente
# Retira". Cancelado vence retirada quando os dois batem.
#
# Fonte: a página de detalhe do pedido (/administrator/inventory/outbound/
# show/{id}), 1 GET por pedido. Testado 28/08: a busca textual da listagem
# (`input_search`) NÃO indexa o id/PS (busca por "31156", "PS-31156" e
# "#PS-31156" devolvem 0), então a listagem não serve pra achar um pedido
# específico. No detalhe, o status é o primeiro `badge-status` logo após
# o rótulo "Situação:" (ex.: "Cancelado", "Enviado" -- os badges seguintes
# são marcadores tipo "Remessa Expressa") e a transportadora vem do bloco
# "Transportadora:" que `_parsear_pagina_detalhe` já lê. Só entra em ação o
# pedido que ainda não está classificado assim; quem já tem OUTRA
# classificação com tratativa concluída não é sobrescrito (só reportado
# como divergente), pra não apagar uma Demanda/duplicação já feita.
#
# Trava de sessão: mesma regra da Torre (buscar_funil_stokki) -- a
# StokkiSession renova login sozinha e um login concorrente derruba a
# sessão de um agente em execução, então a consulta é recusada enquanto
# houver execução RODANDO no painel.
STOKKI_PAUSA_ENTRE_CONSULTAS_SEG = 0.3
_lock_consulta_stokki = threading.Lock()


def _painel_tem_execucao_rodando() -> bool:
    """Mesma checagem de torre_controle._rodando_fora_das_etapas (copiada
    pra não importar o módulo inteiro da Torre aqui)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT 1 FROM painel_execucoes WHERE status='RODANDO' LIMIT 1").fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


def _limpar_html(texto) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(texto or ""))).strip()


_RE_SITUACAO_STOKKI = re.compile(
    r"Situa[çc][ãa]o:\s*</th>\s*<td>\s*<span[^>]*badge-status[^>]*>(.*?)</span>", re.S | re.IGNORECASE,
)


def _status_e_transportadora_stokki(sessao, id_stokki: str) -> dict | None:
    """Abre o detalhe do pedido na Stokki e devolve {"status", "transportadora"}
    -- None se o pedido não existir (404) ou a página não tiver o bloco
    de situação (id que não é um pedido)."""
    from stokki import pedidos as stokki_pedidos

    resp = sessao.get(f"{stokki_pedidos.BASE_URL}/pt-br/administrator/inventory/outbound/show/{id_stokki}")
    # Id inexistente (ex.: order_number que na verdade é a NF do cliente,
    # não resolvido pra id Stokki) dá 500 na Stokki, não 404 -- visto
    # 28/08 com o 53826. Os dois viram "não encontrado".
    if resp.status_code in (404, 500):
        return None
    resp.raise_for_status()
    m = _RE_SITUACAO_STOKKI.search(resp.text)
    if not m:
        return None
    detalhe = stokki_pedidos._parsear_pagina_detalhe(resp.text, int(id_stokki) if str(id_stokki).isdigit() else 0)
    transp = detalhe.get("transportadora") or {}
    return {"status": _limpar_html(m.group(1)), "transportadora": (transp.get("nome") or "").strip()}


def _catalogo_transportadoras():
    """Catálogo da BD_TRANSPORTADORAS (mesmo do pipeline) -- None se a
    planilha não abrir (aí só vale o nome literal)."""
    try:
        from regras.transportadoras import CatalogoTransportadoras
        return CatalogoTransportadoras.carregar(_RAIZ / "dados" / "BD_TRANSPORTADORAS.xlsx")
    except Exception as e:
        logger.warning(f"[pedidos-parados] Sem catálogo de transportadoras ({e}); usando só o nome.")
        return None


def _tipo_retira(nome_transportadora: str, catalogo) -> str | None:
    """
    "literal"  -> transportadora chama "CLIENTE RETIRA" na Stokki: o
                  cliente mesmo busca; vira "Cliente Retira" com o botão
                  "Notificar embarcador" disponível (manual, como sempre).
    "catalogo" -> tipo RETIRADA na BD_TRANSPORTADORAS (transportadora
                  terceira que coleta no galpão, ex.: ACEVILLE): também
                  vira "Cliente Retira" (Hugo, 28/08), mas SEM notificação
                  ao cliente -- a tratativa já nasce concluída.
    None       -> não é retirada.
    """
    nome = re.sub(r"\s+", " ", (nome_transportadora or "")).strip()
    if not nome:
        return None
    if "CLIENTE RETIRA" in nome.upper():
        return "literal"
    if catalogo is not None:
        try:
            if catalogo.resolver(nome).tipo == "RETIRADA":
                return "catalogo"
        except Exception:
            pass
    return None


def verificar_na_stokki(pedidos: list[dict], usuario: str) -> dict:
    """
    `pedidos`: lista de {"order_number", "freshhub_id"} (os que estão em
    tela). Retorna um resumo com o que foi visto e o que foi classificado.
    """
    if not _lock_consulta_stokki.acquire(blocking=False):
        raise RuntimeError("Já existe uma consulta à Stokki em andamento -- aguarde ela terminar.")
    try:
        if _painel_tem_execucao_rodando():
            raise RuntimeError(
                "Há um agente em execução no painel -- consulta à Stokki adiada pra não derrubar a sessão dele."
            )

        from stokki.auth import StokkiSession

        sessao = StokkiSession(_carregar_config())
        catalogo = _catalogo_transportadoras()

        base = _base_externa_com_cache()
        ids_stokki = base.get("ids_stokki", {})
        conn = _conectar()
        try:
            locais = {r["order_number"]: dict(r) for r in conn.execute(
                "SELECT order_number, classificacao, acao_status FROM pedidos_parados_classificacao"
            ).fetchall()}
        finally:
            conn.close()

        resumo = {"consultados": 0, "nao_encontrados": [], "erros": [],
                  "cancelados": [], "cliente_retira": [], "retira_transportadora": [],
                  "divergentes": [], "detalhes": []}
        carimbo = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for i, p in enumerate(pedidos):
            numero = str(p.get("order_number") or "").strip()
            if not numero:
                continue
            id_stokki = ids_stokki.get(numero, numero)
            if i:
                time.sleep(STOKKI_PAUSA_ENTRE_CONSULTAS_SEG)
            try:
                visto = _status_e_transportadora_stokki(sessao, id_stokki)
            except Exception as e:
                logger.warning(f"[pedidos-parados] Stokki falhou pro pedido {numero} (id {id_stokki}): {e}")
                resumo["erros"].append(numero)
                continue
            resumo["consultados"] += 1
            if not visto:
                resumo["nao_encontrados"].append(numero)
                continue

            status = visto["status"]
            transportadora = visto["transportadora"]
            conn = _conectar()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO pedidos_parados_stokki "
                    "(order_number, id_stokki, status, transportadora, consultado_em) VALUES (?, ?, ?, ?, ?)",
                    (numero, str(id_stokki), status, transportadora, carimbo),
                )
                conn.commit()
            finally:
                conn.close()

            alvo = None
            retira = None
            if re.search(r"cancel", status, re.IGNORECASE):
                alvo = "Cancelados"
            else:
                retira = _tipo_retira(transportadora, catalogo)
                if retira:
                    alvo = "Cliente Retira"
            resumo["detalhes"].append({"order_number": numero, "status": status,
                                       "transportadora": transportadora, "sugestao": alvo})
            if not alvo:
                continue

            local = locais.get(numero, {})
            if local.get("classificacao") == alvo:
                continue
            if local.get("classificacao") and local.get("acao_status") == "concluida":
                resumo["divergentes"].append(
                    f"{numero}: Stokki sugere {alvo}, mas já está {local['classificacao']} com tratativa concluída"
                )
                continue

            classificar(numero, str(p.get("freshhub_id") or ""), alvo, f"{usuario} (auto via Stokki)")
            tratativas.registrar_evento(
                numero, "PEDIDOS_PARADOS", "PEDIDO_PARADO_CLASSIFICADO_AUTO_STOKKI",
                decisao=alvo,
                texto=f"Classificado automaticamente como {alvo} por {usuario} -- "
                      f"Stokki: status '{status}', transportadora '{transportadora}'",
            )
            if alvo == "Cancelados":
                resumo["cancelados"].append(numero)
            elif retira == "catalogo":
                # Transportadora terceira que coleta no galpão: sem
                # notificação ao cliente (Hugo, 28/08) -- tratativa já
                # nasce concluída, a tela não oferece "Notificar embarcador".
                _marcar_acao(
                    numero, "concluida",
                    f"transportadora '{transportadora}' é de RETIRADA (BD_TRANSPORTADORAS) -- sem notificação ao cliente",
                )
                resumo["retira_transportadora"].append(numero)
            else:
                resumo["cliente_retira"].append(numero)

        return resumo
    finally:
        _lock_consulta_stokki.release()


# ── Consulta em lote à Vuupt (status + agendamento) ───────────────────────────
#
# Botão "Consultar Vuupt" da triagem (pedido do Hugo, 28/08). Regras dele,
# literais, pra serviço **não atribuído** (status 'not_assigned'):
#   - sem agendamento (scheduled_start vazio)            -> "Em Rota"
#   - agendamento com data de HOJE                       -> "Em Rota"
#   - agendamento com data DEPOIS de hoje                -> "Agendado"
# Lacunas que ele não cobriu, tratadas de forma conservadora (só
# reportadas, sem classificar): agendamento com data ANTERIOR a hoje
# (vencido) e serviço em qualquer outro status (atribuído/em rota/
# concluído/cancelado). Segue a cadeia de reentregas (mesmo fingerprint
# de buscar_sucesso_vuupt) e avalia o serviço MAIS RECENTE da cadeia.
#
# Não sobrescreve classificação que veio de outra fonte (Cancelados,
# Cliente Retira, Reenvio...): só preenche vazio ou troca Em Rota <->
# Agendado entre si -- o resto vira "divergente" no resumo.
#
# Custo: 1 a 3 chamadas na Vuupt por pedido (mesma _resolver_pedido das
# ações), com pausa entre pedidos; é manual e único por clique (não
# entra no ciclo de 60s da tela), e qualquer erro da Vuupt encerra a
# rodada em vez de insistir (lição do 429 de 25/08).
VUUPT_PAUSA_ENTRE_CONSULTAS_SEG = 0.3
CLASSIFICACOES_AUTO_VUUPT = ("Em Rota", "Agendado")
_lock_consulta_vuupt = threading.Lock()


def _data_agendamento_local(scheduled_start) -> date | None:
    if not scheduled_start:
        return None
    try:
        dt = datetime.fromisoformat(str(scheduled_start))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(FUSO_LOCAL)
    return dt.date()


def _servico_mais_recente_da_cadeia(vuupt: VuuptClient, servico: dict) -> dict:
    import fingerprint_duplicacao_insucesso  # sys.path já tem insucesso_entrega/

    atual = servico
    while fingerprint_duplicacao_insucesso.ja_duplicado(atual["id"]):
        novo_code = fingerprint_duplicacao_insucesso.buscar_novo_code(atual["id"])
        if not novo_code:
            break
        reentrega = vuupt.buscar_servico_por_code(novo_code)
        if not reentrega:
            break
        atual = reentrega
    return atual


def _sugestao_vuupt(servico: dict, hoje: date) -> tuple[str | None, str]:
    """(classificação sugerida ou None, motivo legível)."""
    status = servico.get("status") or ""
    if status != "not_assigned":
        return None, f"status '{status}' (não é 'não atribuído')"
    data_ag = _data_agendamento_local(servico.get("scheduled_start"))
    if data_ag is None:
        return "Em Rota", "não atribuído, sem agendamento"
    if data_ag == hoje:
        return "Em Rota", f"não atribuído, agendado pra hoje ({data_ag:%d/%m})"
    if data_ag > hoje:
        return "Agendado", f"não atribuído, agendado pra {data_ag:%d/%m/%Y}"
    return None, f"não atribuído, agendamento vencido ({data_ag:%d/%m/%Y})"


def verificar_na_vuupt(pedidos: list[dict], usuario: str) -> dict:
    """`pedidos`: lista de {"order_number", "freshhub_id"} (os que estão em
    tela). Retorna resumo com o que foi visto e o que foi classificado."""
    if not _lock_consulta_vuupt.acquire(blocking=False):
        raise RuntimeError("Já existe uma consulta à Vuupt em andamento -- aguarde ela terminar.")
    try:
        vuupt = _vuupt()
        hoje = datetime.now(FUSO_LOCAL).date()
        conn = _conectar()
        try:
            locais = {r["order_number"]: dict(r) for r in conn.execute(
                "SELECT order_number, classificacao, acao_status FROM pedidos_parados_classificacao"
            ).fetchall()}
        finally:
            conn.close()

        resumo = {"consultados": 0, "nao_encontrados": [], "erros": [], "interrompido": None,
                  "em_rota": [], "agendado": [], "sem_acao": [], "divergentes": [], "detalhes": []}
        carimbo = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for i, p in enumerate(pedidos):
            numero = str(p.get("order_number") or "").strip()
            if not numero:
                continue
            if i:
                time.sleep(VUUPT_PAUSA_ENTRE_CONSULTAS_SEG)
            try:
                servico, _code = _resolver_pedido(vuupt, numero)
                if servico:
                    servico = _servico_mais_recente_da_cadeia(vuupt, servico)
            except Exception as e:
                logger.warning(f"[pedidos-parados] Vuupt falhou no pedido {numero}; parando a rodada: {e}")
                resumo["erros"].append(numero)
                resumo["interrompido"] = f"Vuupt falhou no pedido {numero} ({e}); os seguintes não foram consultados."
                break
            resumo["consultados"] += 1
            if not servico:
                resumo["nao_encontrados"].append(numero)
                continue

            status = servico.get("status") or ""
            scheduled_start = servico.get("scheduled_start") or ""
            code = (servico.get("code") or "").lstrip("#")
            conn = _conectar()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO pedidos_parados_vuupt "
                    "(order_number, code, status, scheduled_start, consultado_em) VALUES (?, ?, ?, ?, ?)",
                    (numero, code, status, scheduled_start, carimbo),
                )
                conn.commit()
            finally:
                conn.close()

            alvo, motivo = _sugestao_vuupt(servico, hoje)
            resumo["detalhes"].append({"order_number": numero, "code": code, "status": status,
                                       "scheduled_start": scheduled_start, "sugestao": alvo, "motivo": motivo})
            if not alvo:
                resumo["sem_acao"].append(f"{numero}: {motivo}")
                continue

            local = locais.get(numero, {})
            atual = local.get("classificacao")
            if atual == alvo:
                continue
            if atual and atual not in CLASSIFICACOES_AUTO_VUUPT:
                resumo["divergentes"].append(f"{numero}: Vuupt sugere {alvo} ({motivo}), mas já está {atual}")
                continue

            classificar(numero, str(p.get("freshhub_id") or ""), alvo, f"{usuario} (auto via Vuupt)")
            tratativas.registrar_evento(
                code or numero, "PEDIDOS_PARADOS", "PEDIDO_PARADO_CLASSIFICADO_AUTO_VUUPT",
                service_id=servico.get("id"), decisao=alvo,
                texto=f"Classificado automaticamente como {alvo} por {usuario} -- Vuupt: {motivo}",
            )
            (resumo["em_rota"] if alvo == "Em Rota" else resumo["agendado"]).append(numero)

        return resumo
    finally:
        _lock_consulta_vuupt.release()