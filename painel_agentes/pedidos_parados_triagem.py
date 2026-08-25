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
import importlib.util
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))

import yaml

import tratativas
from vuupt_client import VuuptClient
from freshhub.auth import FreshHubSession
from freshhub.pedidos_parados import listar_pedidos_parados
from freshhub.tasks import criar_demanda_para_tratativa

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"

CLASSIFICACOES_VALIDAS = ("Cancelados", "Devolução Parcial", "Reenvio", "Agendado", "Descartar")
CLASSIFICACOES_QUE_ENCAMINHAM_OPERACAO = ("Cancelados", "Devolução Parcial")

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
    ordem.

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

    return None, None


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

    Se a busca falhar, devolve um set vazio (não filtra nada) em vez de
    derrubar a listagem inteira por causa disso.
    """
    inicio = date.today() - timedelta(days=dias)
    filtros = [{"field": "completed_at", "operator": "gte", "value": inicio.strftime("%Y-%m-%d")}]
    try:
        servicos = vuupt.listar_servicos(filtros)
    except Exception as e:
        logger.warning(f"Falha ao buscar serviços concluídos recentes (não filtro entregues por segurança): {e}")
        return set()

    return {
        s["code"].lstrip("#")
        for s in servicos
        if s.get("code") and s.get("completed_at") and not s.get("failed_reason_id")
    }


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
    """
    sessao = _sessao_freshhub()
    brutos = listar_pedidos_parados(sessao, limit=LIMITE_HISTORICO_PEDIDOS_PARADOS)
    if not brutos:
        return []

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
    entregues = _codes_entregues_recentes(vuupt)
    pedidos_do_dia = {
        numero: p for numero, p in pedidos_do_dia.items()
        if f"PS-{numero}" not in entregues
    }

    conn = _conectar()
    try:
        locais = {r["order_number"]: dict(r) for r in conn.execute(
            "SELECT * FROM pedidos_parados_classificacao"
        ).fetchall()}
    finally:
        conn.close()

    resultado = []
    for numero, p in pedidos_do_dia.items():
        dias_parado = (dia_mais_recente - _data_local(primeira_vez[numero])).days + 1
        local = locais.get(numero, {})
        resultado.append({
            "order_number": numero,
            "freshhub_id": p["id"],
            "volumes": p["volumes"],
            "recebedor_name": p["recebedor_name"],
            "created_at": p["created_at"],
            "vezes_registrado": contagem[numero],
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
    Ação das tratativas "Cancelados"/"Devolução Parcial": cria a Demanda
    no Fresh Hub (nunca duplica na Vuupt). Resolve o nome do cliente na
    Vuupt só pra usar no título/client_name do card -- se não conseguir,
    cria a Demanda com um título genérico em vez de travar a ação (é
    mais importante a operação ver o card do que travar por causa de um
    nome que não resolveu)."""
    if classificacao not in CLASSIFICACOES_QUE_ENCAMINHAM_OPERACAO:
        raise ValueError(f"Classificação {classificacao!r} não encaminha pra operação.")

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