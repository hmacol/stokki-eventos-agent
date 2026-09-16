# -*- coding: utf-8 -*-
"""
torre_controle.py

Dados da Torre de Controle (cockpit logístico) -- pedido do Hugo,
12/08: "evoluir o planejamento para uma torre de controle completa com
botões de execução, gráficos de andamento de pedidos e de rotas".

Estrutura da tela segue as práticas de torres de comando levantadas na
pesquisa de 12/08 (Bringg, Onfleet, Samsara, OCC de companhias aéreas,
Stephen Few): 5 faixas -- (1) barra do dia + pipeline como stepper com
botão de executar por etapa ("runbook embutido"), (2) KPIs do dia,
(3) andamento por rota + pedidos por status + funil Stokki, (4) fila
de exceções ordenada por severidade ("management by exception": vazia
= tudo bem), (5) tendência de volume. Nenhum alerta sem ação ao lado.

Fontes de dados (todas já usadas em outros pontos do projeto):
  - VUUPT /routes filtrado por start_at (mesmo filtro do mapa_rotas.py)
    com include=services -- progresso parada a parada de cada rota E a
    visão de pedidos do dia (achado 12/08 no primeiro teste: filtrar
    /services por scheduled_start, como o relatorio_operacional faz,
    só enxerga pedidos COM agendamento -- ~15 num dia de ~100 paradas;
    a operação real do dia é o que está nas rotas);
  - VUUPT not_assigned, separado em ATRASADOS (agendamento <= data e
    ainda sem rota -- o número acionável do dia) e pool normal (sem
    agendamento, aguardando a roteirização de amanhã) -- pedido sem
    agendamento é elegível pra qualquer data (roteirizacao_dados.
    elegivel_para_data), então contar tudo como "sem rota do dia"
    superestimaria o problema;
  - VUUPT contagem por completed_at -- tendência de entregas
    finalizadas por dia útil (contagem leve, sem trazer registros);
  - contagem outbound da Stokki (stokki/pedidos.py::contar_pedidos) --
    funil do que ainda NEM chegou na VUUPT (chaves count_open/
    count_separating/count_pack/count_hold/count_waiting_carrier,
    confirmadas ao vivo em 12/08; count_review/processing/
    waiting_approval são da fila de integração e ficam de fora, ver
    pipeline.py);
  - painel_execucoes (executor.py) -- última execução de cada etapa;
  - rascunhos_rota -- planejamento do dia seguinte;
  - VUUPT /routes por start_at de novo, mas numa faixa maior (semana
    corrente / mês corrente) -- média de pedidos por rota (pedido do
    Hugo, 21/08), com cache de 30 min por não ter persistência local de
    rotas passadas (tudo ao vivo na VUUPT).

O funil Stokki tem cache próprio (TTL) e NUNCA busca ao vivo enquanto
algum agente está rodando -- login concorrente na Stokki derruba a
sessão do processo que estiver no meio de uma execução (401 em massa,
aprendido em produção).
"""
import importlib.util
import logging
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "roteirizacao"))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))

import yaml

from vuupt_client import VuuptClient
from rotas_client import listar_rotas
from regras.preferencias_motoristas import CatalogoMotoristas
from mapa_util import extrair_servicos_da_rota
from executor import buscar_ultima_execucao
from motivos_falha import texto_do_motivo
from expedicao import _exclusoes_por_rota, _exclusoes_da_rota, _rota_ids_com_exclusao_no_dia, _rota_do_corpo
from nucleo.normalizacao import janela_utc_do_dia, vuupt_para_local
import rascunhos_rota
import tratativas

logger = logging.getLogger(__name__)

# Etapas do fluxo diário, na ordem operacional -- cada uma aponta pro
# agente correspondente de agentes.py (o stepper da Faixa 1 mostra a
# última execução e o botão de rodar de cada uma). Roteirização usa o
# modo RASCUNHO de propósito: o disparo manual pela torre alimenta a
# tela de planejamento pra revisão, sem criar direto na VUUPT (o job
# automático das 13h continua sendo o criar_rotas_diarias normal).
ETAPAS_PIPELINE = [
    {"agente_id": "somente_importacao",          "titulo": "Importação",   "detalhe": "Stokki → VUUPT"},
    {"agente_id": "criar_rotas_diarias_rascunho", "titulo": "Roteirização", "detalhe": "gera rascunhos p/ revisão"},
    {"agente_id": "gerar_romaneios",             "titulo": "Romaneios",    "detalhe": "PDFs por rota"},
    {"agente_id": "processar_documentos",        "titulo": "Documentos",   "detalhe": "NF/boleto por pedido"},
    {"agente_id": "somente_expedicao",           "titulo": "Expedição",    "detalhe": "entregues → Stokki"},
    {"agente_id": "relatorio_diario",            "titulo": "Relatório",    "detalhe": "resumo por e-mail"},
]

# Rótulos do funil outbound da Stokki, na ordem do fluxo (o que ainda
# não chegou na VUUPT). Chaves confirmadas ao vivo em 12/08.
FUNIL_STOKKI = [
    ("count_open",            "Aberto"),
    ("count_separating",      "Separando"),
    ("count_pack",            "Pronto p/ embalar"),
    ("count_hold",            "Em espera"),
    ("count_waiting_carrier", "Aguardando transportador"),
]

TTL_FUNIL_STOKKI_SEG = 300     # 5 min -- funil upstream muda devagar
TTL_TENDENCIA_SEG    = 600     # 10 min -- 7 chamadas de contagem na VUUPT
TTL_KPIS_DIA_SEG     = 1800    # 30 min -- dia/semana busca rotas com include=services (payload pesado)
TTL_KPIS_SEMANA_SEG  = 1800
TTL_KPIS_MES_SEG     = 7200    # 2h -- faixa maior (até 1 mês de rotas), muda mais devagar
TTL_KPIS_TRIMESTRE_SEG = 43200 # 12h -- até 3 meses de rotas por busca, não vale martelar

FUSO_LOCAL = ZoneInfo("America/Sao_Paulo")

_cache_stokki: dict = {"quando": 0.0, "dados": None}
_cache_tendencia: dict = {}  # data_iso -> {"quando": monotonic, "dados": [...]}
_cache_kpis_periodo: dict = {}  # "dia_atual:..."/"semana_anterior:..." -> {"quando": monotonic, "dados": [rotas_brutas]}
_lock_caches = threading.Lock()

# Último tamanho conhecido da Fila de ação, publicado por buscar_dados_torre
# (11/09): é o que alimenta o badge da Torre no menu lateral
# (contadores_menu.py). buscar_dados_torre NÃO tem cache de topo -- cada
# chamada é uma coleta ao vivo na VUUPT de vários segundos --, então o
# badge NUNCA a chama por dentro de um request: lê este snapshot, que sai
# de graça de toda carga da própria tela da Torre (que já se atualiza
# sozinha) e, quando ninguém está com ela aberta, de uma renovação em
# segundo plano disparada por contadores_menu.
_snapshot_fila_acao: dict = {"quando": 0.0, "data_iso": None, "qtd": None, "criticas": 0}
_modulo_expedir_pedidos = None  # cache do import explícito, ver _expedir_pedidos_raiz()


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _quando_fila(bruto: str | None, utc: bool = False) -> tuple[str | None, float]:
    """Converte um timestamp bruto num rótulo 'dd/mm HH:MM' pra fila de
    ação e um epoch pra ordenar (pedido do Hugo, 17/08: mostrar dia e
    horário da ocorrência e trazer a mais nova pra cima da fila).

    `utc=True` pro timestamp da VUUPT (completed_at, ISO em UTC);
    `utc=False` pro timestamp já local e "naive" do próprio painel
    (execuções do pipeline, formato '%Y-%m-%d %H:%M:%S').

    Sem timestamp = epoch 0.0: o item vai pro fim da própria faixa de
    severidade -- são condições agregadas (sem rota, sem motorista,
    travas), não uma ocorrência pontual com hora própria.
    """
    if not bruto:
        return None, 0.0
    try:
        if utc:
            texto = bruto.strip()
            if texto.endswith("Z"):
                texto = texto[:-1] + "+00:00"
            dt = datetime.fromisoformat(texto)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(FUSO_LOCAL)
        else:
            dt_local = datetime.strptime(bruto, "%Y-%m-%d %H:%M:%S").replace(tzinfo=FUSO_LOCAL)
        return dt_local.strftime("%d/%m %H:%M"), dt_local.timestamp()
    except (ValueError, TypeError):
        return None, 0.0


def _expedir_pedidos_raiz():
    """Import explícito do expedir_pedidos.py da RAIZ (o que roda de
    verdade em produção). `import expedir_pedidos` simples resolveria
    pra cópia dentro de insucesso_entrega/ por causa da ordem do
    sys.path (essa pasta é inserida por último, ver topo do arquivo --
    armadilha já vivida em produção com esse mesmo par de arquivos)."""
    global _modulo_expedir_pedidos
    if _modulo_expedir_pedidos is None:
        caminho = _RAIZ / "expedir_pedidos.py"
        spec = importlib.util.spec_from_file_location("expedir_pedidos_raiz", caminho)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        _modulo_expedir_pedidos = modulo
    return _modulo_expedir_pedidos


def duplicar_pedido_manual(service_id: int, codigo: str,
                           motorista: str | None = None, rota: str | None = None) -> dict:
    """
    Duplica manualmente um pedido com insucesso -- botão 'Duplicar
    pedido' da fila de ação (pedido do Hugo, 17/08: poder disparar a
    reentrega na hora, sem esperar a resposta do remetente por e-mail).

    Usa a MESMA duplicar_servico_por_insucesso do fluxo automático e
    grava no MESMO fingerprint (insucessos_duplicados) -- o badge da
    fila passa a mostrar 'Duplicado' também pra essa duplicação manual,
    e o fluxo automático por e-mail não duplica de novo em cima dela.
    """
    import fingerprint_duplicacao_insucesso  # já no sys.path (insucesso_entrega/)

    if fingerprint_duplicacao_insucesso.ja_duplicado(service_id):
        novo_code = fingerprint_duplicacao_insucesso.buscar_novo_code(service_id)
        return {"ok": False, "erro": f"Esse pedido já foi duplicado antes (→ {novo_code})."}

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    servico_original = vuupt.buscar_servico_por_code(codigo)
    if not servico_original:
        return {"ok": False, "erro": f"Pedido {codigo} não encontrado no VUUPT."}

    modulo = _expedir_pedidos_raiz()
    novo = modulo.duplicar_servico_por_insucesso(vuupt, servico_original)
    if not novo:
        return {"ok": False, "erro": "Falha ao criar a reentrega no VUUPT (ver log de expedição)."}

    novo_code = novo.get("code", "")
    fingerprint_duplicacao_insucesso.marcar_duplicado(service_id, novo_code)
    tratativas.registrar_evento(
        codigo, "TORRE", "REENVIO_MANUAL",
        service_id=service_id, motorista_nome=motorista, rota_nome=rota,
        texto=f"Duplicado manualmente pela Torre → {novo_code}",
    )
    return {"ok": True, "novo_code": novo_code}


def notificar_ocorrencia_manual(service_id: int, codigo: str) -> dict:
    """
    Dispara manualmente a pergunta de reenvio ao remetente pra UM
    insucesso específico -- botão 'Notificar' da fila de ação (pedido
    do Hugo, 25/08: as notificações automáticas de ocorrência foram
    desligadas em expedir_pedidos.py -- notificacoes_automaticas.ativo
    no config.yaml --, esse botão passa a decidir item a item quando
    notificar).

    Usa a MESMA notificar_remetentes do fluxo automático -- reaplica o
    rate-limit real (fingerprint_aguardando_resposta.pode_notificar: 1
    e-mail/dia/pedido até responder) antes de mandar, e a própria
    função já grava a auditoria (tratativas.AVISO_ENVIADO), não precisa
    duplicar isso aqui.
    """
    from notificar_insucesso_aguardando_resposta import identificar_aguardando_resposta, notificar_remetentes

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    servico = vuupt.buscar_servico_por_code(codigo)
    if not servico:
        return {"ok": False, "erro": f"Pedido {codigo} não encontrado no VUUPT."}

    pendentes = identificar_aguardando_resposta([servico])
    if not pendentes:
        return {"ok": False, "erro": "Já foi perguntado hoje sobre esse pedido (ou aguardando resposta) "
                                     "-- só é possível notificar de novo amanhã."}

    resultado = notificar_remetentes(
        pendentes, config.get("email", {}), config.get("resposta_insucesso", {}), modo_teste=False)

    if resultado.get("enviados"):
        return {"ok": True, "mensagem": "E-mail de ocorrência enviado."}
    if resultado.get("sem_email"):
        return {"ok": False, "erro": "Remetente sem e-mail cadastrado."}
    return {"ok": False, "erro": "Falha ao enviar o e-mail (ver log de expedição)."}


# ── Faixa 1: etapas do pipeline ────────────────────────────────────────────────

def montar_etapas_pipeline() -> list[dict]:
    """Última execução de cada etapa do stepper (leitura barata, só o
    SQLite do painel) -- o front chama isso com frequência maior que o
    resto pra dar feedback rápido depois de um clique em 'rodar'."""
    etapas = []
    for etapa in ETAPAS_PIPELINE:
        ultima = buscar_ultima_execucao(etapa["agente_id"])
        etapas.append({
            **etapa,
            "status": ultima["status"] if ultima else None,
            "quando": (ultima.get("finalizado_em") or ultima.get("iniciado_em")) if ultima else None,
            "modo_teste": bool(ultima["modo_teste"]) if ultima else False,
            "execucao_id": ultima["id"] if ultima else None,
            "rodando": bool(ultima and ultima["status"] == "RODANDO"),
        })
    return etapas


def _rodando_fora_das_etapas() -> bool:
    """Qualquer execução RODANDO no painel (inclusive agentes fora do
    stepper, ex: Executar Tudo) usa a sessão Stokki -- e adia a
    renovação do funil (ver buscar_funil_stokki)."""
    db = _RAIZ / "dados" / "dados.db"
    if not db.exists():
        return False
    try:
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT 1 FROM painel_execucoes WHERE status='RODANDO' LIMIT 1"
        ).fetchone()
        conn.close()
        return bool(row)
    except Exception:
        return False


# ── Pedidos do dia (agregado das rotas + pool sem rota) ───────────────────────

def _resumir_servico(s: dict) -> dict:
    return {
        "service_id": s.get("id"),
        "codigo": s.get("code", ""),
        "titulo": (s.get("title") or "")[:90],
        "completed_at": s.get("completed_at"),
    }


def _badges_insucessos(insucessos: list[dict]) -> None:
    """
    Enriquece cada insucesso (in place) com 'badges' vindos do fluxo
    automático de reentrega (insucesso_entrega/): duplicado (reentrega
    já criada), reentrega agendada pra data futura (ex: loja em
    manutenção, 3 dias úteis), cancelado (remetente respondeu por
    e-mail pedindo pra não reenviar) ou aguardando resposta.

    Tudo vem das 3 tabelas de fingerprint em dados/dados.db (chave =
    service_id do serviço ORIGINAL que falhou -- exatamente o serviço
    que aparece na rota da torre). Falha de leitura não derruba a
    torre: badge é enriquecimento, não dado vital.
    """
    ids = [i["service_id"] for i in insucessos if i.get("service_id")]
    db = _RAIZ / "dados" / "dados.db"
    if not ids or not db.exists():
        for i in insucessos:
            i["badges"] = []
        return

    marcas = ",".join("?" * len(ids))
    duplicados, agendadas, aguardando = {}, {}, {}
    try:
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        try:
            for r in conn.execute(
                f"SELECT service_id_original, novo_code, cancelado_em FROM insucessos_duplicados "
                f"WHERE service_id_original IN ({marcas})", ids):
                duplicados[r["service_id_original"]] = dict(r)
        except sqlite3.OperationalError:
            pass  # tabela ainda não existe (fluxo de insucesso nunca rodou)
        try:
            for r in conn.execute(
                f"SELECT service_id, status, data_agendada, novo_code FROM duplicacoes_agendadas "
                f"WHERE service_id IN ({marcas})", ids):
                agendadas[r["service_id"]] = dict(r)
        except sqlite3.OperationalError:
            pass
        try:
            for r in conn.execute(
                f"SELECT service_id, status, duplicado_apos_resposta FROM insucessos_aguardando_resposta "
                f"WHERE service_id IN ({marcas})", ids):
                aguardando[r["service_id"]] = dict(r)
        except sqlite3.OperationalError:
            pass
        conn.close()
    except Exception as e:
        logger.warning(f"[torre] Falha ao ler fingerprints de insucesso: {e}")

    def _data_br(iso: str) -> str:
        try:
            return date.fromisoformat(iso).strftime("%d/%m")
        except (ValueError, TypeError):
            return iso or ""

    for i in insucessos:
        sid = i.get("service_id")
        dup, ag, ar = duplicados.get(sid), agendadas.get(sid), aguardando.get(sid)
        badges = []

        if dup and dup.get("cancelado_em"):
            badges.append({"tipo": "cancelado", "texto": "Reentrega cancelada (resposta e-mail)"})
        elif ag and ag["status"] == "CANCELADO":
            badges.append({"tipo": "cancelado", "texto": "Reentrega cancelada (resposta e-mail)"})
        elif dup:
            # novo_code vazio: registros antigos (a resposta de criação
            # do VUUPT não ecoava o code -- corrigido 12/08 em
            # expedir_pedidos.py::duplicar_servico_por_insucesso) e
            # também o caso legítimo de "só bloquear reduplicação" de
            # ler_respostas_insucesso -- o texto genérico cobre os dois.
            texto = f"Duplicado → {dup['novo_code']}" if dup.get("novo_code") else "Reentrega criada"
            badges.append({"tipo": "duplicado", "texto": texto})
        elif ag and ag["status"] == "EXECUTADO":
            texto = f"Duplicado → {ag['novo_code']}" if ag.get("novo_code") else "Reentrega criada"
            badges.append({"tipo": "duplicado", "texto": texto})
        elif ag and ag["status"] == "PENDENTE":
            badges.append({"tipo": "agendado", "texto": f"Reentrega agendada p/ {_data_br(ag['data_agendada'])}"})
        elif ag and ag["status"] == "FALHOU":
            badges.append({"tipo": "falha", "texto": "Falha ao duplicar reentrega"})

        if ar and ar["status"] == "PENDENTE":
            badges.append({"tipo": "aguardando", "texto": "Aguardando resposta do remetente"})
        elif ar and ar["status"] == "RESPONDIDO" and not badges:
            badges.append({"tipo": "respondido", "texto": "Respondido por e-mail"})

        i["badges"] = badges


def _coletar_backlog(vuupt: VuuptClient, data_alvo: date) -> dict:
    """Classifica os not_assigned da conta: 'atrasados' (agendamento
    <= data_alvo e ainda sem rota -- deviam estar na rua, é o número
    acionável) vs pool normal (sem agendamento, aguardando a próxima
    roteirização) vs futuros (agendados pra frente, é só esperar).

    Desconta quem já está num rascunho de data_alvo (mesmo cuidado que
    planejamento_rotas.py::buscar_dados_planejamento já toma pro pool --
    achado do Hugo, 23/08: pedido colocado num rascunho pelo
    criar_rotas_diarias das 18h continua 'not_assigned' na VUUPT até o
    rascunho ser enviado, então sem esse desconto ele aparecia como
    "sem rota" na Fila de ação mesmo já tendo rota, deixando o alerta
    quase sempre superestimado entre a criação do rascunho e o envio)."""
    try:
        rascunhos_ativos = rascunhos_rota.listar_rascunhos_do_dia(data_alvo)
        ids_em_rascunho = {p["service_id"] for r in rascunhos_ativos for p in r["paradas"]}
    except Exception as e:
        logger.warning(f"[torre] Falha ao ler rascunhos de {data_alvo} pro desconto do backlog: {e}")
        ids_em_rascunho = set()

    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    servicos = vuupt.listar_servicos(filtro, per_page=100)

    atrasados = []
    pool = futuros = 0
    for s in servicos:
        if s.get("id") in ids_em_rascunho:
            continue
        bruto = s.get("scheduled_start")
        data_agendada = None
        if bruto:
            try:
                data_agendada = datetime.fromisoformat(str(bruto)).date()
            except (ValueError, TypeError):
                pass
        if data_agendada is None:
            pool += 1
        elif data_agendada <= data_alvo:
            atrasados.append(_resumir_servico(s))
        else:
            futuros += 1

    return {"atrasados": atrasados, "pool": pool, "futuros": futuros}


def contar_sem_rota(data_alvo: date | None = None) -> dict:
    """Só o número "Sem rota" da torre (atrasados do backlog), pro chip
    do planejamento -- 1 fetch de serviços, sem pagar a visão completa
    de buscar_dados_torre."""
    data_alvo = data_alvo or date.today()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)
    backlog = _coletar_backlog(vuupt, data_alvo)
    return {
        "qtd": len(backlog["atrasados"]),
        "exemplos": [p["codigo"] for p in backlog["atrasados"][:5]],
    }


def _montar_pedidos_dia(agregado: dict, backlog: dict) -> dict:
    """
    Visão de pedidos do dia = paradas das rotas do dia (agregado de
    _coletar_rotas_abertas) + atrasados sem rota. NÃO usa /services por
    scheduled_start: só pedido com agendamento tem esse campo com a
    data certa (achado 12/08 -- o filtro enxergava 15 pedidos num dia
    de ~100 paradas).
    """
    qtd_atrasados = len(backlog["atrasados"])
    return {
        "total": agregado["total"] + qtd_atrasados,
        "contagem": {
            "on_route": agregado["em_rota"],
            "accepted": agregado["aceitos"],
            "not_assigned": qtd_atrasados,
            "canceled": agregado["cancelados"],
        },
        "sucesso": agregado["entregues"],
        "falha": agregado["insucessos"],
        "nao_atribuidos": backlog["atrasados"][:30],
        "qtd_nao_atribuidos": qtd_atrasados,
        "backlog_pool": backlog["pool"],
        "backlog_futuros": backlog["futuros"],
        "insucessos": agregado["insucessos_lista"][:30],
    }


_PADRAO_NUMERO = re.compile(r"(\d+)")


def _chave_ordem_natural(texto: str) -> list:
    """Quebra o nome em pedaços texto/número pra comparar dígitos pelo
    valor numérico, não caractere a caractere (achado do Hugo, 23/08:
    'Planejamento - ... - #11' ordenava antes de '#2' porque '1' < '2'
    como string). Cada pedaço vira (0, int) ou (1, str) pra tupla nunca
    comparar int com str quando dois nomes têm estrutura diferente."""
    pedacos = _PADRAO_NUMERO.split(texto)
    return [(0, int(p)) if p.isdigit() else (1, p.lower()) for p in pedacos if p != ""]


# ── Rotas do dia (VUUPT /routes) ──────────────────────────────────────────────

# Uma rota que acabou de ter um pedido excluído fica destacada no topo
# de "Andamento das rotas" por um tempo (pedido do Hugo, 25/08: excluir
# o ÚLTIMO pendente de uma rota vira "Concluída" na hora e pula pro fim
# da lista -- de longe parece que a rota sumiu, achado em produção no
# mesmo dia). Por TEMPO DE PAREDE desde o registro da exclusão
# (criado_em), não por sessão de navegador -- mais de 1 pessoa pode
# estar olhando a Torre ao mesmo tempo, e assim vale igual pra
# qualquer uma que abrir a tela dentro da janela.
MINUTOS_DESTAQUE_EXCLUSAO = 30


def _teve_exclusao_recente(exclusoes: list[dict], agora: datetime) -> bool:
    for ex in exclusoes:
        try:
            quando = datetime.fromisoformat(ex["criado_em"])
        except (TypeError, ValueError):
            continue
        if agora - quando <= timedelta(minutes=MINUTOS_DESTAQUE_EXCLUSAO):
            return True
    return False


def _montar_card_rota_cancelada(token: str, data_alvo: date, rota_id: int,
                                nomes_motoristas: dict[int, str]) -> dict:
    """Reconstrói o card (formato Torre) de uma rota que sumiu de
    listar_rotas porque foi cancelada de vez (perdeu a última parada
    ATIVA por um "Excluir da Rota", ver expedicao.excluir_pedido_da_
    rota) -- sem paradas ativas, só o(s) chip(s) excluído(s). Mesmo
    padrão e mesmo motivo da Expedição (ver expedicao._montar_card_
    rota_cancelada): o pedido não pode sumir da tela só porque a rota
    em si deixou de existir na VUUPT. Busca a rota direto por ID pra
    pegar nome/motorista atualizados -- GET /routes/{id} devolve a
    rota mesmo cancelada (só a LISTAGEM filtra); se nem isso responder
    (rota apagada de vez, não só cancelada), cai pro nome gravado na
    própria exclusão."""
    from rotas_client import buscar_rota

    nome, motorista = None, None
    try:
        rota = _rota_do_corpo(buscar_rota(token, rota_id, include=["agent"]))
        nome = rota.get("name")
        agent_id = rota.get("agent_id")
        motorista = nomes_motoristas.get(agent_id) if agent_id else None
    except Exception:
        pass

    exclusoes = _exclusoes_por_rota(data_alvo, rota_id)
    if not nome:
        nome = (exclusoes[0]["rota_nome"] if exclusoes and exclusoes[0].get("rota_nome") else f"Rota {rota_id}")

    vistos = set()
    pedidos_chip = []
    for ex in exclusoes:
        sid = ex["service_id"]
        if sid in vistos:
            continue
        vistos.add(sid)
        pedidos_chip.append({
            "ordem": None, "codigo": ex["codigo_pedido"] or "", "titulo": "",
            "situacao": "excluido", "service_id": sid,
            "motivo": ex["motivo"], "observacao": ex["observacao"],
        })

    return {
        "id": rota_id, "nome": nome, "motorista": motorista,
        "data_rota": data_alvo.isoformat(), "data_rota_br": data_alvo.strftime("%d/%m/%Y"),
        "total": 0, "entregues": 0, "insucessos": 0, "restantes": 0, "percentual": 0,
        "estado": "vazia", "paradas": [], "pedidos": pedidos_chip, "cancelada": True,
        "excluido_recente": _teve_exclusao_recente(exclusoes, datetime.now()),
    }


# ── Torre acumulada: o que fica e o que vai pro Histórico ────────────────────

# Quantos dias pra trás a torre procura rota ainda aberta (Hugo, 15/09:
# "a torre não é filtrada por data, é um acúmulo das datas"). Rota mais
# velha que isso e ainda sem encerrar é caso pra olhar no Histórico
# (/consulta), não pra ficar na tela pra sempre.
DIAS_JANELA_TORRE = 14


def classificar_rota_torre(rota: dict, hoje: date, ids_tratadas: set[str],
                           ids_encerradas: set[int]) -> dict:
    """Regra pura (Hugo, 15/09, opção A): a rota FICA na torre enquanto
    tiver parada por resolver OU insucesso ainda não tratado na Fila de
    ação. Sai (já está no Histórico pelo espelho do núcleo) quando tudo
    resolvido e sem pendência, ou quando alguém clicou "Encerrar".

    `ids_tratadas` são os ids de torre_excecoes_tratadas ('insucesso:
    <codigo>'); `ids_encerradas` são os rota_id de torre_rotas_encerradas.
    Devolve {"fica", "atrasada", "pendencias"} -- atrasada = rota de dia
    anterior que ainda não fechou (destaque + botão Encerrar na tela).
    """
    data_rota = date.fromisoformat(rota["data_rota"]) if rota.get("data_rota") else hoje
    pendencias = sum(
        1 for p in rota.get("pedidos", [])
        if p.get("situacao") == "insucesso" and f"insucesso:{p.get('codigo')}" not in ids_tratadas
    )
    if rota.get("id") in ids_encerradas:
        return {"fica": False, "atrasada": False, "pendencias": pendencias}

    if rota.get("cancelada"):
        # Card reconstruído só pra manter o chip excluído visível: vale
        # no próprio dia, depois é história.
        return {"fica": data_rota == hoje, "atrasada": False, "pendencias": 0}

    aberta = rota.get("estado") in ("em_andamento", "nao_iniciada")
    fica = aberta or pendencias > 0
    return {"fica": fica, "atrasada": fica and data_rota < hoje, "pendencias": pendencias}


def _data_local_da_rota(rota: dict, padrao: date) -> date:
    """Dia LOCAL em que a rota começa. start_at da VUUPT vem em UTC sem
    fuso (armadilha conhecida: rota das 22h de SP cai no dia seguinte
    em UTC) -- mesma conversão que o espelho do núcleo usa."""
    local = vuupt_para_local(rota.get("start_at"))
    try:
        return date.fromisoformat(str(local)[:10])
    except (TypeError, ValueError):
        return padrao


def _contrib_vazia() -> dict:
    return {"total": 0, "entregues": 0, "insucessos": 0, "em_rota": 0,
            "aceitos": 0, "cancelados": 0, "insucessos_lista": []}


def _montar_card_rota(rota: dict, data_rota: date, nomes_motoristas: dict[int, str],
                      agora: datetime) -> tuple[dict, dict]:
    """Card de UMA rota (formato Torre) + a contribuição dela pro agregado
    de pedidos. Separado da coleta (15/09) porque a torre acumulada só
    soma no agregado as rotas que FICAM na tela (ver classificar_rota_
    torre) -- a decisão vem depois de montar o card."""
    contrib = _contrib_vazia()
    # Motorista resolvido antes do loop de paradas: é ele quem
    # registra a atualização de status no app -- cada insucesso da
    # fila de ação carrega o responsável (pedido do Hugo, 13/08).
    agent_id = rota.get("agent_id")
    motorista = nomes_motoristas.get(agent_id) if agent_id else None
    servicos = extrair_servicos_da_rota(rota)
    contrib["cancelados"] += sum(1 for s in servicos if s.get("status") == "canceled")
    validos = [s for s in servicos if s.get("status") != "canceled"]
    total = len(validos)
    entregues = insucessos = em_rota = 0
    paradas_mapa = []
    pedidos_chip = []
    for ordem, s in enumerate(validos, start=1):
        status = s.get("status")
        if status == "done":
            if s.get("status_done") == "failed":
                insucessos += 1
                contrib["insucessos_lista"].append({
                    **_resumir_servico(s),
                    "motorista": motorista,
                    "rota": rota.get("name", ""),
                    "motivo_insucesso": texto_do_motivo(s.get("failed_reason_id")),
                })
                # Preenche motorista das tratativas já registradas pra
                # este pedido, se ainda não tinha (pedido do Hugo,
                # 14/08: "capturar motorista daqui pra frente") --
                # aproveita o que a Torre já resolveu (agent_id->nome)
                # sem nenhuma chamada nova à VUUPT.
                if s.get("code") and motorista:
                    tratativas.enriquecer_motorista(s["code"], motorista, rota.get("name", ""))
                situacao = "insucesso"
            else:
                entregues += 1
                situacao = "entregue"
        elif status == "on_route":
            em_rota += 1
            situacao = "em_rota"
        else:
            if status == "accepted":
                contrib["aceitos"] += 1
            situacao = "pendente"

        # Chip por pedido (visão alternativa da torre, pedido do
        # Hugo, 23/08) -- ao contrário de paradas_mapa, entra TODO
        # pedido válido, com ou sem coordenada. `ordem` é a posição
        # da parada dentro da rota (1, 2, 3...), a MESMA numeração
        # exibida no planejamento -- é o que o Hugo chama de
        # "Número da Ordem de Entrega" (não confundir com `codigo`,
        # o PS-XXXXX interno da VUUPT).
        pedidos_chip.append({
            "ordem": ordem,
            "codigo": s.get("code", ""),
            "titulo": (s.get("title") or "")[:70],
            "situacao": situacao,
            "service_id": s.get("id"),
        })

        # Parada georreferenciada pro mini mapa -- serviço sem
        # coordenada fica fora do mapa, mas conta em tudo acima.
        lat, lng = s.get("latitude"), s.get("longitude")
        try:
            if lat not in (None, "") and lng not in (None, ""):
                paradas_mapa.append({
                    "lat": float(lat), "lng": float(lng),
                    "codigo": s.get("code", ""),
                    "titulo": (s.get("title") or "")[:70],
                    "situacao": situacao,
                })
        except (TypeError, ValueError):
            pass

    # Pedido excluído da rota por aqui ("Excluir da Rota" do menu de
    # contexto do chip, pedido do Hugo, 25/08 -- mesmo botão da
    # Expedição, ver expedicao.excluir_pedido_da_rota chamado com
    # permitir_rota_em_andamento=True) nunca some do chip: mesma
    # regra da Expedição, fica marcado "excluido" (motivo no hover)
    # em vez de sumir. Se era a ÚLTIMA parada ATIVA da rota, ela é
    # cancelada de vez na VUUPT e some desta listagem -- reconstruída
    # à parte (ver _montar_card_rota_cancelada). Exclusões de QUALQUER
    # dia: rota de ontem ainda aberta pode ter exclusão feita hoje.
    exclusoes_rota = _exclusoes_da_rota(rota.get("id"))
    ids_ativos = {p["service_id"] for p in pedidos_chip}
    vistos_excluidos = set()
    for ex in exclusoes_rota:
        sid = ex["service_id"]
        if sid in ids_ativos or sid in vistos_excluidos:
            continue
        vistos_excluidos.add(sid)
        pedidos_chip.append({
            "ordem": None, "codigo": ex["codigo_pedido"] or "", "titulo": "",
            "situacao": "excluido", "service_id": sid,
            "motivo": ex["motivo"], "observacao": ex["observacao"],
        })

    finalizados = entregues + insucessos
    contrib["total"] += total
    contrib["entregues"] += entregues
    contrib["insucessos"] += insucessos
    contrib["em_rota"] += em_rota

    if total == 0:
        estado = "vazia"
    elif finalizados >= total:
        estado = "concluida"
    elif finalizados > 0 or em_rota > 0:
        estado = "em_andamento"
    else:
        estado = "nao_iniciada"

    card = {
        "id": rota.get("id"),
        "nome": rota.get("name", ""),
        "motorista": motorista,
        "data_rota": data_rota.isoformat(),
        "data_rota_br": data_rota.strftime("%d/%m/%Y"),
        "total": total,
        "entregues": entregues,
        "insucessos": insucessos,
        "restantes": max(0, total - finalizados),
        "percentual": round(finalizados / total * 100) if total else 0,
        "estado": estado,
        "paradas": paradas_mapa,
        "pedidos": pedidos_chip,
        "cancelada": False,
        "excluido_recente": _teve_exclusao_recente(exclusoes_rota, agora),
    }
    return card, contrib


def _coletar_rotas_abertas(token: str, hoje: date, nomes_motoristas: dict[int, str],
                           ids_encerradas: set[int]) -> tuple[list[dict], dict, list[dict]]:
    """Rotas ainda ABERTAS de qualquer dia da janela (Hugo, 15/09: a
    torre deixa de ser filtrada por data e acumula; rota concluída sem
    pendência já está no Histórico pelo espelho do núcleo). Todas as
    rotas da VUUPT na janela, não só as com prefixo 'Planejamento' do
    mapa -- a torre precisa enxergar também rota criada na mão.

    Retorna (rotas, agregado, rotas_brutas_hoje): o agregado soma as
    paradas só das rotas que FICARAM (é a base da visão de pedidos,
    _montar_pedidos_dia); rotas_brutas_hoje são as rotas cruas de HOJE
    (canceladas inclusas, _estatisticas_periodo filtra) pra alimentar o
    KPI "dia" sem 2ª chamada à API.
    """
    inicio, _ = janela_utc_do_dia(hoje - timedelta(days=DIAS_JANELA_TORRE))
    _, fim = janela_utc_do_dia(hoje)
    filtro = [
        {"field": "start_at", "operator": "gte", "value": inicio},
        {"field": "start_at", "operator": "lt", "value": fim},
    ]
    rotas_brutas = listar_rotas(token, include=["services"], filtro=filtro)

    agora = datetime.now()
    candidatas: list[tuple[dict, dict]] = []
    rotas_brutas_hoje = []
    for rota in rotas_brutas:
        data_rota = _data_local_da_rota(rota, hoje)
        if data_rota == hoje:
            rotas_brutas_hoje.append(rota)
        if rota.get("status") == "canceled":
            continue
        candidatas.append(_montar_card_rota(rota, data_rota, nomes_motoristas, agora))

    # Rota que perdeu a ÚLTIMA parada ATIVA por uma exclusão feita por
    # aqui (chip -> "Excluir da Rota") vira "canceled" na VUUPT e por
    # isso foi pulada no loop acima -- reconstrói um card mínimo só pra
    # manter visível o(s) chip(s) excluído(s), mesmo padrão da Expedição
    # (ver expedicao._montar_card_rota_cancelada). Só de HOJE: depois
    # disso é história (classificar_rota_torre também descarta).
    ids_com_dados = {card["id"] for card, _ in candidatas}
    for rota_id in _rota_ids_com_exclusao_no_dia(hoje) - ids_com_dados:
        candidatas.append((_montar_card_rota_cancelada(token, hoje, rota_id, nomes_motoristas), _contrib_vazia()))

    # Insucesso já tratado na Fila de ação não segura a rota na torre
    # (opção A do Hugo, 15/09) -- 1 leitura no SQLite pra todos os ids.
    ids_insucesso = sorted({
        f"insucesso:{p.get('codigo')}" for card, _ in candidatas
        for p in card["pedidos"] if p.get("situacao") == "insucesso"
    })
    ids_tratadas = set(_buscar_tratadas(ids_insucesso))

    rotas = []
    agregado = _contrib_vazia()
    for card, contrib in candidatas:
        veredito = classificar_rota_torre(card, hoje, ids_tratadas, ids_encerradas)
        if not veredito["fica"]:
            continue
        card["atrasada"] = veredito["atrasada"]
        card["pendencias"] = veredito["pendencias"]
        rotas.append(card)
        for chave in ("total", "entregues", "insucessos", "em_rota", "aceitos", "cancelados"):
            agregado[chave] += contrib[chave]
        agregado["insucessos_lista"].extend(contrib["insucessos_lista"])

    # Rota com exclusão recente primeiro de tudo (ver MINUTOS_DESTAQUE_
    # EXCLUSAO acima) -- excluir o último pendente fecha a rota em
    # "Concluída" na hora, que sem isso pularia direto pro fim da
    # lista, dando a impressão de ter sumido (achado do Hugo, 25/08).
    # Depois por dia, hoje primeiro (a tela agrupa por data); dentro de
    # cada dia: em andamento primeiro (é onde a atenção deve estar),
    # depois as que ainda nem saíram, concluídas com pendência por
    # último; empate por nome em ordem NUMÉRICA (_chave_ordem_natural),
    # não alfabética -- 'Planejamento - DD/MM/AAAA - #N' ordenava '#11'
    # antes de '#2' (achado do Hugo, 23/08).
    ordem_estado = {"em_andamento": 0, "nao_iniciada": 1, "concluida": 2, "vazia": 3}
    rotas.sort(key=lambda r: (
        0 if r.get("excluido_recente") else 1,
        r["data_rota"] != hoje.isoformat(),
        -date.fromisoformat(r["data_rota"]).toordinal(),
        ordem_estado.get(r["estado"], 9),
        _chave_ordem_natural(r["nome"]),
    ))
    return rotas, agregado, rotas_brutas_hoje


# ── Tendência (7 dias úteis, com cache) ───────────────────────────────────────

def _eh_fim_de_semana(dia: date) -> bool:
    return dia.weekday() >= 5


def _coletar_tendencia(vuupt: VuuptClient, ultimo_dia: date, dias: int = 7) -> list[dict]:
    """Entregas FINALIZADAS (done, sucesso+falha) por dia útil, via
    contagem por completed_at (leve, não traz registros) -- mesmo campo
    já usado em expedir_pedidos.py. Não usa scheduled_start como o
    relatorio_operacional: só pedido com agendamento tem esse campo
    (ver _montar_pedidos_dia)."""
    resultado = []
    dia = ultimo_dia
    while len(resultado) < dias:
        if not _eh_fim_de_semana(dia):
            filtro = [
                {"field": "completed_at", "operator": "gte", "value": dia.strftime("%Y-%m-%d")},
                {"field": "completed_at", "operator": "lt", "value": (dia + timedelta(days=1)).strftime("%Y-%m-%d")},
            ]
            try:
                total = vuupt.contar_servicos(filtro)
            except Exception as e:
                logger.error(f"[torre] Falha ao contar tendência do dia {dia}: {e}")
                total = 0
            resultado.append({"rotulo": dia.strftime("%d/%m"), "data": dia.isoformat(), "total": total})
        dia -= timedelta(days=1)
    resultado.reverse()
    return resultado


def _tendencia_com_cache(vuupt: VuuptClient, data_alvo: date) -> list[dict]:
    chave = data_alvo.isoformat()
    with _lock_caches:
        item = _cache_tendencia.get(chave)
        if item and time.monotonic() - item["quando"] < TTL_TENDENCIA_SEG:
            return item["dados"]
    dados = _coletar_tendencia(vuupt, data_alvo)
    with _lock_caches:
        _cache_tendencia.clear()  # só interessa a data em uso; não acumula
        _cache_tendencia[chave] = {"quando": time.monotonic(), "dados": dados}
    return dados


# ── KPIs por período (dia/semana/mês/trimestre, atual x anterior, com cache) ──
#
# Generaliza o que só existia pra "pedidos por rota" (21/08) pra todos os
# KPIs -- e acrescenta o período ANTERIOR equivalente de cada aba, pra dar
# a variação (Δ) que a Torre nunca teve. "Equivalente" importa: mês/
# trimestre correntes são sempre parciais (hoje pode ser dia 23), então o
# anterior usa o MESMO número de dias decorridos, nunca o período anterior
# inteiro -- senão a comparação é enganosa (23 dias de agosto vs os 31 de
# julho puxaria a taxa/volume pra baixo por motivo nenhum).

def _inicio_semana(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _inicio_mes(d: date) -> date:
    return d.replace(day=1)


def _inicio_trimestre(d: date) -> date:
    mes_inicio = ((d.month - 1) // 3) * 3 + 1
    return d.replace(month=mes_inicio, day=1)


def _subtrai_meses(d: date, n: int) -> date:
    """d precisa já ser dia 1 de algum mês (início de mês ou de
    trimestre) -- volta n meses, sempre devolvendo outro dia 1."""
    indice = d.year * 12 + (d.month - 1) - n
    return date(indice // 12, indice % 12 + 1, 1)


def _janela_anterior_equivalente(inicio_atual: date, data_alvo: date, subtrair) -> tuple[date, date]:
    """(início, fim exclusivo) do período anterior com o MESMO número de
    dias decorridos que [inicio_atual, data_alvo] tem hoje."""
    dias_decorridos = (data_alvo - inicio_atual).days + 1
    inicio_anterior = subtrair(inicio_atual)
    fim_anterior_exclusivo = inicio_anterior + timedelta(days=dias_decorridos)
    return inicio_anterior, fim_anterior_exclusivo


def _duracao_rota_min(start_at_bruto, completed_ats: list[str]) -> float | None:
    """Minutos entre o início real da rota (campo start_at, confirmado
    como real em incrementar_rotas.py::_data_inicio_rota) e a última
    parada finalizada. None se alguma data faltar ou não parsear -- não
    quebra o agregado do período por causa de 1 rota com dado ruim."""
    if not start_at_bruto or not completed_ats:
        return None
    try:
        inicio = datetime.fromisoformat(str(start_at_bruto))
        fim = max(datetime.fromisoformat(str(c)) for c in completed_ats)
    except (ValueError, TypeError):
        return None
    minutos = (fim - inicio).total_seconds() / 60
    return minutos if minutos > 0 else None


def _estatisticas_periodo(rotas_brutas: list[dict]) -> dict:
    """Agregado leve sobre rotas cruas da API -- sem chips, mini mapa,
    badges nem gravação de tratativas (isso é só da visão rica de
    "hoje", _coletar_rotas_abertas). Usado pra semana/mês/trimestre (atual
    e anterior) e também pro "hoje", reaproveitando as MESMAS
    rotas_brutas que _coletar_rotas_abertas já buscou (0 chamadas extras)."""
    total = entregues = insucessos = 0
    rotas_total = rotas_concluidas = rotas_com_carga = 0
    motivos = Counter()
    duracoes_min = []
    motoristas_distintos = set()

    for rota in rotas_brutas:
        if rota.get("status") == "canceled":
            continue
        rotas_total += 1
        validos = [s for s in extrair_servicos_da_rota(rota) if s.get("status") != "canceled"]
        if not validos:
            continue

        finalizados_em = []
        rota_entregues = rota_insucessos = 0
        for s in validos:
            if s.get("status") == "done":
                if s.get("status_done") == "failed":
                    rota_insucessos += 1
                    motivos[texto_do_motivo(s.get("failed_reason_id"))] += 1
                else:
                    rota_entregues += 1
                if s.get("completed_at"):
                    finalizados_em.append(s["completed_at"])

        total += len(validos)
        entregues += rota_entregues
        insucessos += rota_insucessos
        rotas_com_carga += 1
        agent_id = rota.get("agent_id")
        if agent_id:
            motoristas_distintos.add(agent_id)

        finalizados = rota_entregues + rota_insucessos
        if finalizados >= len(validos):
            rotas_concluidas += 1
            duracao = _duracao_rota_min(rota.get("start_at"), finalizados_em)
            if duracao is not None:
                duracoes_min.append(duracao)

    top_motivos = [{"motivo": m, "qtd": q} for m, q in motivos.most_common(5)]
    if len(motivos) > 5:
        resto = sum(q for _, q in motivos.most_common()[5:])
        top_motivos.append({"motivo": "Outros", "qtd": resto})

    return {
        "entregues": entregues,
        "total_paradas": total,
        "sucesso_pct": round(100 * entregues / (entregues + insucessos), 1) if (entregues + insucessos) else None,
        "insucessos": insucessos,
        "rotas_total": rotas_total,
        "rotas_concluidas": rotas_concluidas,
        "ped_rota": round(total / rotas_com_carga, 1) if rotas_com_carga else 0,
        "duracao_media_min": round(sum(duracoes_min) / len(duracoes_min)) if duracoes_min else None,
        "motivos_insucesso": top_motivos,
        "motoristas_distintos": len(motoristas_distintos),
    }


def _rotas_periodo_com_cache(token: str, chave: str, data_inicio: date, data_fim_exclusiva: date, ttl: int) -> list[dict]:
    with _lock_caches:
        item = _cache_kpis_periodo.get(chave)
        if item and time.monotonic() - item["quando"] < ttl:
            return item["dados"]
    filtro = [
        {"field": "start_at", "operator": "gte", "value": data_inicio.strftime("%Y-%m-%d") + " 00:00:00"},
        {"field": "start_at", "operator": "lt", "value": data_fim_exclusiva.strftime("%Y-%m-%d") + " 00:00:00"},
    ]
    rotas_brutas = listar_rotas(token, include=["services"], filtro=filtro)
    with _lock_caches:
        _cache_kpis_periodo[chave] = {"quando": time.monotonic(), "dados": rotas_brutas}
    return rotas_brutas


def _bloco_periodo(token: str, inicio_atual: date, data_alvo: date,
                   subtrair, ttl: int, prefixo: str, rotas_brutas_atual: list[dict] | None = None) -> dict:
    """{"atual": ..., "anterior": ...} -- estatísticas do período atual
    (segunda até hoje, dia 1 até hoje, etc.) e do período anterior
    equivalente (_janela_anterior_equivalente). rotas_brutas_atual
    reaproveita o que _coletar_rotas_abertas já buscou pro "hoje"; None faz
    a busca própria (cacheada por `ttl` segundos)."""
    fim_atual_exclusivo = data_alvo + timedelta(days=1)
    if rotas_brutas_atual is None:
        rotas_brutas_atual = _rotas_periodo_com_cache(
            token, f"{prefixo}_atual:{inicio_atual.isoformat()}:{data_alvo.isoformat()}",
            inicio_atual, fim_atual_exclusivo, ttl)

    inicio_anterior, fim_anterior_exclusivo = _janela_anterior_equivalente(inicio_atual, data_alvo, subtrair)
    rotas_brutas_anterior = _rotas_periodo_com_cache(
        token, f"{prefixo}_anterior:{inicio_anterior.isoformat()}:{fim_anterior_exclusivo.isoformat()}",
        inicio_anterior, fim_anterior_exclusivo, ttl)

    return {
        "atual": _estatisticas_periodo(rotas_brutas_atual),
        "anterior": _estatisticas_periodo(rotas_brutas_anterior),
    }


def _montar_kpis_periodo(token: str, data_alvo: date, rotas_brutas_hoje: list[dict],
                         motoristas_ativos: int) -> dict:
    """As 4 abas da régua de KPIs. `motoristas_ativos` (denominador fixo
    do catálogo) transforma motoristas_distintos em % de utilização --
    calculado aqui fora, não dentro de _estatisticas_periodo, porque não
    depende do período nenhum, só do cadastro atual."""
    blocos = {
        "dia": _bloco_periodo(token, data_alvo, data_alvo,
                              lambda d: d - timedelta(days=1), TTL_KPIS_DIA_SEG, "dia",
                              rotas_brutas_atual=rotas_brutas_hoje),
        "semana": _bloco_periodo(token, _inicio_semana(data_alvo), data_alvo,
                                 lambda d: d - timedelta(days=7), TTL_KPIS_SEMANA_SEG, "semana"),
        "mes": _bloco_periodo(token, _inicio_mes(data_alvo), data_alvo,
                              lambda d: _subtrai_meses(d, 1), TTL_KPIS_MES_SEG, "mes"),
        "trimestre": _bloco_periodo(token, _inicio_trimestre(data_alvo), data_alvo,
                                    lambda d: _subtrai_meses(d, 3), TTL_KPIS_TRIMESTRE_SEG, "trimestre"),
    }
    for bloco in blocos.values():
        for chave in ("atual", "anterior"):
            distintos = bloco[chave]["motoristas_distintos"]
            bloco[chave]["utilizacao_motoristas_pct"] = (
                round(100 * distintos / motoristas_ativos, 1) if motoristas_ativos else None)
    return blocos


# ── Exceções tratadas (persistência) ──────────────────────────────────────────

def _conectar_tratadas():
    """Tabela própria da torre em dados/dados.db: exceções marcadas
    como tratadas pelo operador (pedido do Hugo, 12/08 -- padrão OCC da
    pesquisa: 'o item só sai da fila quando alguém clicar tratado, com
    motivo, gerando histórico')."""
    db = _RAIZ / "dados" / "dados.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS torre_excecoes_tratadas (
            id          TEXT PRIMARY KEY,
            data_alvo   TEXT,
            tipo        TEXT,
            descricao   TEXT,
            motivo      TEXT,
            tratado_em  TEXT NOT NULL
        )
    """)
    # Rota encerrada na mão pela torre acumulada (Hugo, 15/09): rota de
    # dia anterior que nunca saiu (ou nunca vai fechar) sumiria só
    # quando a janela DIAS_JANELA_TORRE passasse -- o botão "Encerrar"
    # tira ela da tela na hora. Não mexe na VUUPT nem no núcleo: é só
    # a torre deixando de mostrar.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS torre_rotas_encerradas (
            rota_id      INTEGER PRIMARY KEY,
            rota_nome    TEXT,
            data_rota    TEXT,
            usuario      TEXT,
            encerrada_em TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def encerrar_rota_torre(rota_id: int, rota_nome: str, data_rota: str, usuario: str) -> None:
    conn = _conectar_tratadas()
    conn.execute("""
        INSERT INTO torre_rotas_encerradas (rota_id, rota_nome, data_rota, usuario, encerrada_em)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(rota_id) DO UPDATE SET usuario = excluded.usuario, encerrada_em = excluded.encerrada_em
    """, (int(rota_id), (rota_nome or "")[:200], (data_rota or "")[:10], (usuario or "")[:100],
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()


def _buscar_encerradas() -> set[int]:
    try:
        conn = _conectar_tratadas()
        rows = conn.execute("SELECT rota_id FROM torre_rotas_encerradas").fetchall()
        conn.close()
        return {int(r["rota_id"]) for r in rows}
    except Exception as e:
        logger.warning(f"[torre] Falha ao ler rotas encerradas: {e}")
        return set()


def _pedido_code_da_excecao(excecao_id: str) -> str | None:
    """Só exceções do tipo 'Insucesso' (id = 'insucesso:<code>') se
    referem a um pedido -- as outras ('semrota:', 'semmotorista:',
    'travas:', 'pipeline:') não têm um pedido único por trás e ficam de
    fora do histórico de tratativas por pedido."""
    if excecao_id and excecao_id.startswith("insucesso:"):
        return excecao_id.split(":", 1)[1]
    return None


def marcar_excecao_tratada(excecao_id: str, data_alvo: str, tipo: str,
                           descricao: str, motivo: str,
                           motorista_nome: str | None = None, rota_nome: str | None = None):
    conn = _conectar_tratadas()
    conn.execute("""
        INSERT INTO torre_excecoes_tratadas (id, data_alvo, tipo, descricao, motivo, tratado_em)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET motivo = excluded.motivo, tratado_em = excluded.tratado_em
    """, (excecao_id, data_alvo, tipo, (descricao or "")[:300], (motivo or "")[:500],
          datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

    pedido_code = _pedido_code_da_excecao(excecao_id)
    if pedido_code:
        tratativas.registrar_evento(
            pedido_code, "TORRE", "EXCECAO_TRATADA",
            motorista_nome=motorista_nome, rota_nome=rota_nome, texto=motivo,
        )


def desfazer_excecao_tratada(excecao_id: str) -> bool:
    """Desfaz um 'tratado' (clique errado) -- a linha some do histórico
    de propósito: tratado desfeito nunca aconteceu. O log de tratativas
    (append-only, auditoria) recebe um evento novo em vez de apagar."""
    conn = _conectar_tratadas()
    cur = conn.execute("DELETE FROM torre_excecoes_tratadas WHERE id = ?", (excecao_id,))
    conn.commit()
    conn.close()

    pedido_code = _pedido_code_da_excecao(excecao_id)
    if pedido_code:
        tratativas.registrar_evento(pedido_code, "TORRE", "EXCECAO_DESTRATADA", texto="(desfeito)")

    return cur.rowcount > 0


def _buscar_tratadas(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    try:
        conn = _conectar_tratadas()
        marcas = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT * FROM torre_excecoes_tratadas WHERE id IN ({marcas})", ids
        ).fetchall()
        conn.close()
        return {r["id"]: dict(r) for r in rows}
    except Exception as e:
        logger.warning(f"[torre] Falha ao ler exceções tratadas: {e}")
        return {}


# ── Fila de exceções ──────────────────────────────────────────────────────────

def _montar_excecoes(pedidos: dict, rotas: list[dict], etapas: list[dict],
                     amanha: dict, data_alvo: date) -> tuple[list[dict], list[dict]]:
    """Shortlist ordenada por severidade (padrão OCC: vazia = tudo bem).
    Cada item carrega a própria ação -- 'nenhum alerta sem botão do
    lado' (pesquisa 12/08) -- e um id ESTÁVEL pro 'marcar como tratado'
    (o mesmo problema não volta pra fila depois de tratado; um problema
    NOVO gera id novo e reaparece: exceções agrupadas levam a contagem
    no id de propósito, pra ressurgirem se o número mudar).

    Retorna (ativas, tratadas) -- tratadas vêm com motivo/tratado_em.
    """
    excecoes = []
    data_iso = data_alvo.isoformat()

    for e in etapas:
        if e["status"] in ("ERRO", "TIMEOUT"):
            quando, epoch = _quando_fila(e["quando"], utc=False)
            excecoes.append({
                "id": f"pipeline:{e['agente_id']}:{e['execucao_id']}",
                "severidade": "critico",
                "tipo": "Pipeline",
                "descricao": f"Etapa '{e['titulo']}' terminou em {e['status']}.",
                "quando": quando,
                "acao": {"tipo": "rodar", "agente_id": e["agente_id"], "rotulo": "Reexecutar",
                         "agente_titulo": e["titulo"]},
                "link_log": f"/execucao/{e['execucao_id']}" if e["execucao_id"] else None,
                "_epoch": epoch,
            })

    try:
        from fingerprint_aguardando_resposta import pode_notificar as _pode_notificar_ocorrencia
    except Exception:
        _pode_notificar_ocorrencia = None

    for i in pedidos["insucessos"]:
        quando, epoch = _quando_fila(i.get("completed_at"), utc=True)
        badges = i.get("badges", [])
        sid = i.get("service_id")
        # Botão 'Notificar' (pedido do Hugo, 25/08: notificações
        # automáticas de ocorrência desligadas, esse botão passa a
        # decidir). Reaplica o MESMO rate-limit do fluxo automático
        # (1 e-mail/dia/pedido até responder) só pra decidir se o
        # botão aparece -- a checagem de verdade roda de novo na hora
        # do clique (notificar_ocorrencia_manual).
        if _pode_notificar_ocorrencia and sid:
            try:
                pode_notificar_ocorrencia = _pode_notificar_ocorrencia(sid)
            except Exception as e:
                logger.warning(f"[torre] Falha ao checar rate-limit de notificação p/ {sid}: {e}")
                pode_notificar_ocorrencia = True
        else:
            pode_notificar_ocorrencia = bool(sid)
        excecoes.append({
            "id": f"insucesso:{i['codigo']}",
            "severidade": "critico",
            "tipo": "Insucesso",
            "descricao": f"{i['codigo']} — {i['titulo']}",
            "motivo_insucesso": i.get("motivo_insucesso") or "Motivo não informado",
            "quando": quando,
            # Quem registrou a atualização: o motorista da rota (é ele
            # que marca o insucesso no app). 'rota' é o fallback pra
            # rota sem motorista atribuído.
            "motorista": i.get("motorista"),
            "rota": i.get("rota"),
            "badges": badges,
            "service_id": i.get("service_id"),
            "codigo": i.get("codigo"),
            "pode_notificar_ocorrencia": pode_notificar_ocorrencia,
            # Botão 'Duplicar pedido' (pedido do Hugo, 17/08): some
            # quando já existe uma reentrega executada (badge
            # 'duplicado') -- duplicar de novo criaria uma segunda.
            # Reentrega só AGENDADA/aguardando resposta ainda pode ser
            # antecipada na hora por aqui.
            "pode_duplicar": not any(b.get("tipo") == "duplicado" for b in badges),
            "acao": None,
            "_epoch": epoch,
        })

    if pedidos["qtd_nao_atribuidos"]:
        excecoes.append({
            "id": f"semrota:{data_iso}:{pedidos['qtd_nao_atribuidos']}",
            "severidade": "atencao",
            "tipo": "Sem rota",
            "descricao": f"{pedidos['qtd_nao_atribuidos']} pedido(s) com agendamento até hoje e ainda sem rota "
                         f"(ex: {', '.join(p['codigo'] for p in pedidos['nao_atribuidos'][:5])}).",
            "quando": None,
            "acao": {"tipo": "link", "url": f"/planejamento?data={data_iso}", "rotulo": "Planejar"},
            "_epoch": 0.0,
        })

    sem_motorista = [r for r in rotas if not r["motorista"] and r["estado"] not in ("concluida", "vazia")]
    for r in sem_motorista:
        excecoes.append({
            "id": f"semmotorista:{r['id']}",
            "severidade": "atencao",
            "tipo": "Sem motorista",
            "descricao": f"Rota '{r['nome']}' ({r['total']} parada(s)) sem motorista atribuído.",
            "quando": None,
            "acao": {"tipo": "link", "url": f"/mapa-rotas?data={r.get('data_rota') or data_iso}", "rotulo": "Ver rota"},
            "_epoch": 0.0,
        })

    if amanha.get("qtd_travas"):
        excecoes.append({
            "id": f"travas:{amanha['data_iso']}:{amanha['qtd_travas']}",
            "severidade": "atencao",
            "tipo": "Planejamento",
            "descricao": f"{amanha['qtd_travas']} rascunho(s) de amanhã com trava estourada (paradas/caixas/distância).",
            "quando": None,
            "acao": {"tipo": "link", "url": f"/planejamento?data={amanha['data_iso']}", "rotulo": "Revisar"},
            "_epoch": 0.0,
        })

    tratadas_por_id = _buscar_tratadas([x["id"] for x in excecoes])
    ativas, tratadas = [], []
    for x in excecoes:
        epoch = x.pop("_epoch")
        registro = tratadas_por_id.get(x["id"])
        if registro:
            tratadas.append({**x, "motivo": registro["motivo"], "tratado_em": registro["tratado_em"]})
        else:
            ativas.append((x, epoch))

    # Severidade primeiro (crítico antes de atenção -- é onde a decisão
    # é mais urgente), ocorrência mais nova primeiro dentro da mesma
    # faixa (pedido do Hugo, 17/08: "incluir novas ocorrências sempre
    # em primeiro lugar" -- sem isso, um insucesso novo podia nascer no
    # meio da lista, atrás de insucessos antigos do mesmo dia).
    ordem = {"critico": 0, "atencao": 1}
    ativas.sort(key=lambda par: (ordem.get(par[0]["severidade"], 9), -par[1]))
    tratadas.sort(key=lambda x: x["tratado_em"], reverse=True)
    return [x for x, _ in ativas], tratadas


# ── Planejamento de amanhã (rascunhos) ────────────────────────────────────────

def _resumo_amanha(data_alvo: date, token: str) -> dict:
    """Resumo do planejamento do dia seguinte à data da torre -- o
    fluxo real planeja amanhã hoje (job das 13h / botão Roteirização).
    Olha os rascunhos locais E as rotas já criadas direto na VUUPT
    (o job automático ainda cria lá, sem passar por rascunho)."""
    from planejamento_rotas import _badges_trava

    dia_seguinte = data_alvo + timedelta(days=1)
    try:
        rascunhos = rascunhos_rota.listar_rascunhos_do_dia(dia_seguinte)
    except Exception as e:
        logger.warning(f"[torre] Falha ao ler rascunhos de {dia_seguinte}: {e}")
        rascunhos = []

    qtd_travas = 0
    for r in rascunhos:
        try:
            if _badges_trava(r):
                qtd_travas += 1
        except Exception:
            pass

    # Sem include=services de propósito: só a contagem interessa aqui,
    # e o payload de rotas com serviços é pesado.
    rotas_vuupt = 0
    try:
        filtro = [
            {"field": "start_at", "operator": "gte", "value": dia_seguinte.strftime("%Y-%m-%d") + " 00:00:00"},
            {"field": "start_at", "operator": "lt", "value": (dia_seguinte + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"},
        ]
        rotas_vuupt = sum(1 for r in listar_rotas(token, filtro=filtro) if r.get("status") != "canceled")
    except Exception as e:
        logger.warning(f"[torre] Falha ao contar rotas de {dia_seguinte} na VUUPT: {e}")

    return {
        "data_iso": dia_seguinte.isoformat(),
        "data_rotulo": dia_seguinte.strftime("%d/%m"),
        "qtd_rotas": len(rascunhos),
        "qtd_paradas": sum(len(r["paradas"]) for r in rascunhos),
        "sem_motorista": sum(1 for r in rascunhos if not r.get("agent_id")),
        "qtd_travas": qtd_travas,
        "rotas_vuupt": rotas_vuupt,
    }


# ── Funil Stokki (cache + trava de sessão) ────────────────────────────────────

def buscar_funil_stokki(forcar: bool = False) -> dict:
    """
    Contagem outbound da Stokki com cache de TTL_FUNIL_STOKKI_SEG.

    NUNCA renova ao vivo enquanto algum agente do painel está RODANDO:
    a StokkiSession renova login sozinha quando a sessão expira, e um
    login concorrente derruba a sessão do agente em execução (401 em
    massa -- visto em produção). Nesses momentos serve o cache antigo
    marcado como 'adiado'.
    """
    with _lock_caches:
        idade = time.monotonic() - _cache_stokki["quando"]
        cache_valido = _cache_stokki["dados"] is not None and idade < TTL_FUNIL_STOKKI_SEG
    if cache_valido and not forcar:
        return _cache_stokki["dados"]

    if _rodando_fora_das_etapas():
        if _cache_stokki["dados"]:
            return {**_cache_stokki["dados"], "adiado": True}
        return {"ok": False, "adiado": True, "estagios": [],
                "erro": "Agente em execução -- consulta à Stokki adiada pra não derrubar a sessão."}

    try:
        # Imports locais: só paga o custo (bs4/playwright na cadeia da
        # sessão) quando o funil é de fato consultado.
        from stokki.auth import StokkiSession
        from stokki.pedidos import contar_pedidos

        sessao = StokkiSession(_carregar_config())
        bruto = contar_pedidos(sessao, "all")
        estagios = [
            {"chave": chave, "rotulo": rotulo, "total": int(bruto.get(chave) or 0)}
            for chave, rotulo in FUNIL_STOKKI
        ]
        dados = {
            "ok": True,
            "adiado": False,
            "estagios": estagios,
            "total": sum(e["total"] for e in estagios),
            "atualizado_em": datetime.now().strftime("%H:%M:%S"),
        }
    except Exception as e:
        logger.warning(f"[torre] Falha ao consultar contagem da Stokki: {e}")
        if _cache_stokki["dados"]:
            return {**_cache_stokki["dados"], "adiado": True}
        return {"ok": False, "adiado": False, "estagios": [], "erro": str(e)}

    with _lock_caches:
        _cache_stokki["quando"] = time.monotonic()
        _cache_stokki["dados"] = dados
    return dados


# ── Visão completa ────────────────────────────────────────────────────────────

def buscar_dados_torre(data_alvo: date | None = None) -> dict:
    """Tudo que a torre mostra, menos o funil Stokki (endpoint próprio,
    com cache e trava de sessão) -- 1 fetch de serviços + 1 de rotas +
    tendência cacheada + leituras locais.

    Desde 15/09 (Hugo) a torre não é filtrada por data: as rotas são as
    ainda abertas de qualquer dia da janela (_coletar_rotas_abertas);
    `data_alvo` é só a âncora de HOJE pros KPIs de período, tendência,
    "sem rota" e "amanhã" (default: date.today())."""
    data_alvo = data_alvo or date.today()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    cfg_motoristas = config.get("motoristas", {})
    catalogo = None
    try:
        catalogo = CatalogoMotoristas.carregar(
            cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
        nomes_motoristas = {m.agent_id: m.nome for m in catalogo.motoristas if m.agent_id}
    except Exception as e:
        logger.warning(f"[torre] Falha ao carregar catálogo de motoristas: {e}")
        nomes_motoristas = {}
    motoristas_ativos = sum(1 for m in catalogo.motoristas if m.ativo) if catalogo else 0

    rotas, agregado, rotas_brutas_hoje = _coletar_rotas_abertas(
        token, data_alvo, nomes_motoristas, _buscar_encerradas())
    backlog = _coletar_backlog(vuupt, data_alvo)
    pedidos = _montar_pedidos_dia(agregado, backlog)
    _badges_insucessos(pedidos["insucessos"])
    etapas = montar_etapas_pipeline()
    amanha = _resumo_amanha(data_alvo, token)
    tendencia = _tendencia_com_cache(vuupt, data_alvo)
    kpis_periodo = _montar_kpis_periodo(token, data_alvo, rotas_brutas_hoje, motoristas_ativos)
    excecoes, tratadas = _montar_excecoes(pedidos, rotas, etapas, amanha, data_alvo)

    # Base do mini mapa (mesmo endereço/geocache do mapa_rotas.py).
    base = None
    try:
        from geocodificacao import geocodificar
        from mapa_rotas import ENDERECO_BASE
        coords_base = geocodificar(ENDERECO_BASE, config.get("google_maps", {}).get("api_key", ""))
        if coords_base:
            base = {"lat": coords_base[0], "lng": coords_base[1]}
    except Exception as e:
        logger.warning(f"[torre] Falha ao geocodificar a base: {e}")

    rotas_por_estado = Counter(r["estado"] for r in rotas)
    qtd_criticos = sum(1 for x in excecoes if x["severidade"] == "critico")
    qtd_atencao = len(excecoes) - qtd_criticos
    semaforo = "critico" if qtd_criticos else ("atencao" if qtd_atencao else "ok")

    # Badge da Torre no menu lateral aproveita esta coleta (ver
    # _snapshot_fila_acao) -- de graça, sem nenhuma chamada a mais.
    with _lock_caches:
        _snapshot_fila_acao.update({
            "quando": time.monotonic(), "data_iso": data_alvo.isoformat(),
            "qtd": len(excecoes), "criticas": qtd_criticos,
        })

    return {
        "data_alvo": data_alvo.strftime("%d/%m/%Y"),
        "data_alvo_iso": data_alvo.isoformat(),
        "gerado_em": datetime.now().strftime("%H:%M:%S"),
        "semaforo": semaforo,
        "pedidos": pedidos,
        "rotas": rotas,
        "rotas_resumo": {
            "concluidas": rotas_por_estado.get("concluida", 0),
            "em_andamento": rotas_por_estado.get("em_andamento", 0),
            "nao_iniciadas": rotas_por_estado.get("nao_iniciada", 0),
            "atrasadas": sum(1 for r in rotas if r.get("atrasada")),
        },
        "janela_dias": DIAS_JANELA_TORRE,
        "etapas": etapas,
        "amanha": amanha,
        "tendencia": tendencia,
        "kpis_periodo": kpis_periodo,
        "excecoes": excecoes,
        "tratadas": tratadas,
        "base": base,
    }


def snapshot_fila_acao(data_alvo: date) -> dict | None:
    """Último tamanho conhecido da Fila de ação pra `data_alvo`, sem
    disparar coleta nenhuma (ver _snapshot_fila_acao). Devolve None se
    ainda não há leitura, ou se a última é de outro dia.

    {"qtd": int, "criticas": int, "idade_seg": float}
    """
    with _lock_caches:
        snap = dict(_snapshot_fila_acao)
    if snap["qtd"] is None or snap["data_iso"] != data_alvo.isoformat():
        return None
    return {"qtd": snap["qtd"], "criticas": snap["criticas"],
            "idade_seg": time.monotonic() - snap["quando"]}
