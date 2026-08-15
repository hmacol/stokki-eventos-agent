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
  - rascunhos_rota -- planejamento do dia seguinte.

O funil Stokki tem cache próprio (TTL) e NUNCA busca ao vivo enquanto
algum agente está rodando -- login concorrente na Stokki derruba a
sessão do processo que estiver no meio de uma execução (401 em massa,
aprendido em produção).
"""
import logging
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

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

TTL_FUNIL_STOKKI_SEG = 300   # 5 min -- funil upstream muda devagar
TTL_TENDENCIA_SEG    = 600   # 10 min -- 7 chamadas de contagem na VUUPT

_cache_stokki: dict = {"quando": 0.0, "dados": None}
_cache_tendencia: dict = {}  # data_iso -> {"quando": monotonic, "dados": [...]}
_lock_caches = threading.Lock()


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


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
    roteirização) vs futuros (agendados pra frente, é só esperar)."""
    filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"}]
    servicos = vuupt.listar_servicos(filtro, per_page=100)

    atrasados = []
    pool = futuros = 0
    for s in servicos:
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


def _montar_pedidos_dia(agregado: dict, backlog: dict) -> dict:
    """
    Visão de pedidos do dia = paradas das rotas do dia (agregado de
    _coletar_rotas_dia) + atrasados sem rota. NÃO usa /services por
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


# ── Rotas do dia (VUUPT /routes) ──────────────────────────────────────────────

def _coletar_rotas_dia(token: str, data_alvo: date,
                       nomes_motoristas: dict[int, str]) -> tuple[list[dict], dict]:
    """Progresso parada a parada de cada rota do dia (todas as rotas da
    data, não só as com prefixo 'Planejamento' do mapa -- a torre
    precisa enxergar também rota criada na mão).

    Retorna (rotas, agregado): o agregado soma as paradas de TODAS as
    rotas do dia e é a base da visão de pedidos (_montar_pedidos_dia).
    """
    inicio = data_alvo.strftime("%Y-%m-%d") + " 00:00:00"
    fim = (data_alvo + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    filtro = [
        {"field": "start_at", "operator": "gte", "value": inicio},
        {"field": "start_at", "operator": "lt", "value": fim},
    ]
    rotas_brutas = listar_rotas(token, include=["services"], filtro=filtro)

    rotas = []
    agregado = {"total": 0, "entregues": 0, "insucessos": 0, "em_rota": 0,
                "aceitos": 0, "cancelados": 0, "insucessos_lista": []}
    for rota in rotas_brutas:
        if rota.get("status") == "canceled":
            continue
        # Motorista resolvido antes do loop de paradas: é ele quem
        # registra a atualização de status no app -- cada insucesso da
        # fila de ação carrega o responsável (pedido do Hugo, 13/08).
        agent_id = rota.get("agent_id")
        motorista = nomes_motoristas.get(agent_id) if agent_id else None
        servicos = extrair_servicos_da_rota(rota)
        agregado["cancelados"] += sum(1 for s in servicos if s.get("status") == "canceled")
        validos = [s for s in servicos if s.get("status") != "canceled"]
        total = len(validos)
        entregues = insucessos = em_rota = 0
        paradas_mapa = []
        for s in validos:
            status = s.get("status")
            if status == "done":
                if s.get("status_done") == "failed":
                    insucessos += 1
                    agregado["insucessos_lista"].append({
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
                    agregado["aceitos"] += 1
                situacao = "pendente"

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
        finalizados = entregues + insucessos

        agregado["total"] += total
        agregado["entregues"] += entregues
        agregado["insucessos"] += insucessos
        agregado["em_rota"] += em_rota

        if total == 0:
            estado = "vazia"
        elif finalizados >= total:
            estado = "concluida"
        elif finalizados > 0 or em_rota > 0:
            estado = "em_andamento"
        else:
            estado = "nao_iniciada"

        rotas.append({
            "id": rota.get("id"),
            "nome": rota.get("name", ""),
            "motorista": motorista,
            "total": total,
            "entregues": entregues,
            "insucessos": insucessos,
            "restantes": max(0, total - finalizados),
            "estado": estado,
            "paradas": paradas_mapa,
        })

    # Em andamento primeiro (é onde a atenção deve estar), depois as que
    # ainda nem saíram, concluídas por último; empate por nome.
    ordem_estado = {"em_andamento": 0, "nao_iniciada": 1, "concluida": 2, "vazia": 3}
    rotas.sort(key=lambda r: (ordem_estado.get(r["estado"], 9), r["nome"]))
    return rotas, agregado


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
    conn.commit()
    return conn


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
            excecoes.append({
                "id": f"pipeline:{e['agente_id']}:{e['execucao_id']}",
                "severidade": "critico",
                "tipo": "Pipeline",
                "descricao": f"Etapa '{e['titulo']}' terminou em {e['status']} ({e['quando'] or 'sem horário'}).",
                "acao": {"tipo": "rodar", "agente_id": e["agente_id"], "rotulo": "Reexecutar",
                         "agente_titulo": e["titulo"]},
                "link_log": f"/execucao/{e['execucao_id']}" if e["execucao_id"] else None,
            })

    for i in pedidos["insucessos"]:
        # Sem botão de ação próprio (o tratamento já é automático:
        # duplicação na hora + respostas por e-mail) -- os badges
        # mostram em que pé o fluxo automático está.
        excecoes.append({
            "id": f"insucesso:{i['codigo']}",
            "severidade": "critico",
            "tipo": "Insucesso",
            "descricao": f"{i['codigo']} — {i['titulo']} ({i.get('motivo_insucesso') or 'Motivo não informado'})",
            # Quem registrou a atualização: o motorista da rota (é ele
            # que marca o insucesso no app). 'rota' é o fallback pra
            # rota sem motorista atribuído.
            "motorista": i.get("motorista"),
            "rota": i.get("rota"),
            "badges": i.get("badges", []),
            "acao": None,
        })

    if pedidos["qtd_nao_atribuidos"]:
        excecoes.append({
            "id": f"semrota:{data_iso}:{pedidos['qtd_nao_atribuidos']}",
            "severidade": "atencao",
            "tipo": "Sem rota",
            "descricao": f"{pedidos['qtd_nao_atribuidos']} pedido(s) com agendamento até hoje e ainda sem rota "
                         f"(ex: {', '.join(p['codigo'] for p in pedidos['nao_atribuidos'][:5])}).",
            "acao": {"tipo": "link", "url": f"/planejamento?data={data_iso}", "rotulo": "Planejar"},
        })

    sem_motorista = [r for r in rotas if not r["motorista"] and r["estado"] not in ("concluida", "vazia")]
    for r in sem_motorista:
        excecoes.append({
            "id": f"semmotorista:{r['id']}",
            "severidade": "atencao",
            "tipo": "Sem motorista",
            "descricao": f"Rota '{r['nome']}' ({r['total']} parada(s)) sem motorista atribuído.",
            "acao": {"tipo": "link", "url": f"/mapa-rotas?data={data_iso}", "rotulo": "Ver rota"},
        })

    if amanha.get("qtd_travas"):
        excecoes.append({
            "id": f"travas:{amanha['data_iso']}:{amanha['qtd_travas']}",
            "severidade": "atencao",
            "tipo": "Planejamento",
            "descricao": f"{amanha['qtd_travas']} rascunho(s) de amanhã com trava estourada (paradas/caixas/distância).",
            "acao": {"tipo": "link", "url": f"/planejamento?data={amanha['data_iso']}", "rotulo": "Revisar"},
        })

    tratadas_por_id = _buscar_tratadas([x["id"] for x in excecoes])
    ativas, tratadas = [], []
    for x in excecoes:
        registro = tratadas_por_id.get(x["id"])
        if registro:
            tratadas.append({**x, "motivo": registro["motivo"], "tratado_em": registro["tratado_em"]})
        else:
            ativas.append(x)

    ordem = {"critico": 0, "atencao": 1}
    ativas.sort(key=lambda x: ordem.get(x["severidade"], 9))
    tratadas.sort(key=lambda x: x["tratado_em"], reverse=True)
    return ativas, tratadas


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
    tendência cacheada + leituras locais."""
    data_alvo = data_alvo or date.today()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)

    cfg_motoristas = config.get("motoristas", {})
    try:
        catalogo = CatalogoMotoristas.carregar(
            cfg_motoristas.get("planilha", ""), cfg_motoristas.get("json_fallback", ""))
        nomes_motoristas = {m.agent_id: m.nome for m in catalogo.motoristas if m.agent_id}
    except Exception as e:
        logger.warning(f"[torre] Falha ao carregar catálogo de motoristas: {e}")
        nomes_motoristas = {}

    rotas, agregado = _coletar_rotas_dia(token, data_alvo, nomes_motoristas)
    backlog = _coletar_backlog(vuupt, data_alvo)
    pedidos = _montar_pedidos_dia(agregado, backlog)
    _badges_insucessos(pedidos["insucessos"])
    etapas = montar_etapas_pipeline()
    amanha = _resumo_amanha(data_alvo, token)
    tendencia = _tendencia_com_cache(vuupt, data_alvo)
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
        },
        "etapas": etapas,
        "amanha": amanha,
        "tendencia": tendencia,
        "excecoes": excecoes,
        "tratadas": tratadas,
        "base": base,
    }
