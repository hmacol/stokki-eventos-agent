# -*- coding: utf-8 -*-
"""
portal_cliente/dados_cliente.py

Dados que o embarcador vê no portal de acompanhamento (pedido do Hugo,
08/09): os pedidos DELE no dia (rotas da VUUPT filtradas por sender_id),
o que ainda não saiu do galpão, insucessos aguardando resposta, próximos
dias agendados e um resumo dos últimos 30 dias.

Fontes -- as mesmas da Torre de Controle (painel_agentes/torre_controle.py),
sem reaproveitar buscar_dados_torre() porque aquela função traz a conta
inteira (KPIs, funil Stokki, tendência, geocodificação) a cada chamada;
aqui só o recorte de um remetente:
  - VUUPT /routes por start_at do dia, include=services (+customer, agent):
    situação parada a parada. A ordem da parada é a posição na lista
    services.data da rota, sem os cancelados -- a mesma numeração que a
    Torre e o planejamento chamam de "Ordem de Entrega".
  - VUUPT /services status=not_assigned (+sender_id): o que ainda não tem
    rota -- sem agendamento = "aguardando saída"; agendado pra frente =
    "agendado" (alimenta "próximos dias"). Mesmos critérios de
    torre_controle._coletar_backlog.
  - insucessos_aguardando_resposta (dados.db): insucessos cujo motivo pede
    decisão do embarcador (o bloco "precisa da sua atenção"); a resposta
    passa por insucesso_entrega/aplicar_resposta_insucesso.aplicar_decisao,
    o MESMO caminho da página /insucesso.
  - documentos_processados (dados.db): número da NF por código base do
    pedido (mesma query de painel_agentes/rascunhos_rota.py).
  - VUUPT /services status=done + sender_id + completed_at >= 30 dias:
    resumo do período, cacheado por 1 h por remetente.

Tudo cacheado por (sender_id, data) por 5 min -- o portal diz na tela
"sincroniza a cada 5 min".
"""
import logging
import re
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_RAIZ = Path(__file__).parent.parent
for _p in (_RAIZ, _RAIZ / "roteirizacao", _RAIZ / "insucesso_entrega"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import requests
import yaml

from http_retry import chamar_com_retry
from vuupt_client import VuuptClient
from rotas_client import listar_rotas
from regras.preferencias_motoristas import CatalogoMotoristas
from motivos_falha import texto_do_motivo, pergunta_do_motivo

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"
CANHOTOS_DIR = _RAIZ / "dados" / "canhotos"
API_BASE = "https://api.vuupt.com/api/v1"
FUSO = ZoneInfo("America/Sao_Paulo")

CACHE_DIA_SEGUNDOS = 5 * 60
CACHE_HISTORICO_SEGUNDOS = 60 * 60
HISTORICO_DIAS = 30
PROXIMOS_DIAS_MAX = 4

_PADRAO_CODIGO_BASE = re.compile(r"PS-\d+", re.IGNORECASE)

# Rótulos que o cliente vê. "programado" = já está numa rota do dia que
# ainda não saiu (assigned/accepted na VUUPT); "aguardando_saida" = ainda
# sem rota (not_assigned sem agendamento futuro).
ROTULOS = {
    "entregue": "Entregue",
    "em_rota": "Em rota",
    "programado": "Programado",
    "aguardando_saida": "Aguardando saída",
    "agendado": "Agendado",
    "insucesso": "Insucesso",
}


def carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Helpers ────────────────────────────────────────────────────────────────────

def codigo_base(codigo: str) -> str:
    """'#PS-36327-R1' -> 'PS-36327' (mesma regra de planejamento_rotas)."""
    m = _PADRAO_CODIGO_BASE.search((codigo or "").lstrip("#").strip())
    return m.group(0).upper() if m else (codigo or "").lstrip("#")


def codigos_base_lista(codigo: str) -> list[str]:
    return [codigo_base(c.strip()) for c in (codigo or "").split(",") if c.strip()]


def _parse_dt(valor) -> datetime | None:
    """Datas da VUUPT vêm como ISO com offset ('2026-09-08T10:42:00-03:00')
    ou 'YYYY-MM-DD HH:MM:SS'. Devolve datetime no fuso de São Paulo
    (naive) ou None."""
    if not valor:
        return None
    try:
        dt = datetime.fromisoformat(str(valor).replace(" ", "T").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(FUSO).replace(tzinfo=None)
    return dt


def _hora(valor) -> str:
    dt = _parse_dt(valor)
    return dt.strftime("%H:%M") if dt else ""


def _data_agendada(servico: dict) -> date | None:
    dt = _parse_dt(servico.get("scheduled_start"))
    return dt.date() if dt else None


def _volumes(servico: dict) -> int:
    try:
        return max(int(float(servico.get("dimension_3") or 0)), 1)
    except (TypeError, ValueError):
        return 1


def _servicos_da_rota(rota: dict) -> list[dict]:
    w = rota.get("services")
    if isinstance(w, dict):
        return w.get("data", []) or []
    return w if isinstance(w, list) else []


def _desembrulhar(obj, chave: str):
    """include=customer/agent vem como {"data": {...}} ou direto."""
    v = (obj or {}).get(chave)
    if isinstance(v, dict) and isinstance(v.get("data"), dict):
        return v["data"]
    return v if isinstance(v, dict) else {}


def _janela(cliente: dict) -> str:
    ini, fim = str(cliente.get("operating_hour_start") or "")[:5], str(cliente.get("operating_hour_end") or "")[:5]
    if not ini or not fim or (ini == "00:00" and fim in ("24:00", "23:59")):
        return ""  # dia inteiro = sem janela
    return f"{ini}–{fim}"


_PADRAO_CEP = re.compile(r"^\d{5}-?\d{3}$")


def _endereco_curto(endereco: str) -> str:
    """'Rua X 120 - Apto 44 Vila P, Vila P, Santo Andre - SP, 09190-250, Brasil'
    -> 'Rua X 120 - Apto 44 Vila P, Santo Andre - SP' (sem CEP/país e sem
    parte repetida). O completo fica em endereco_completo."""
    partes = []
    for p in (endereco or "").split(","):
        p = p.strip()
        if not p or p.lower() in ("brasil", "brazil") or _PADRAO_CEP.match(p):
            continue
        if partes and (p.lower() == partes[-1].lower() or partes[-1].lower().endswith(p.lower())):
            continue
        partes.append(p)
    return ", ".join(partes)


_PADRAO_ROTA_PLANEJAMENTO = re.compile(r"^Planejamento\s*-\s*\d{2}/\d{2}/\d{4}\s*-\s*#?(\d+)", re.IGNORECASE)


def _rota_curta(nome: str, rota_id) -> str:
    """'Planejamento - 08/09/2026 - #12' -> 'Rota 12'; outros nomes ficam."""
    m = _PADRAO_ROTA_PLANEJAMENTO.match(nome or "")
    if m:
        return f"Rota {m.group(1)}"
    return nome or f"Rota {rota_id}"


def _ddmm(d: date) -> str:
    return d.strftime("%d/%m")


def _dia_semana(d: date) -> str:
    return ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"][d.weekday()]


def _conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def nf_por_codigo(codigos_base: set[str]) -> dict[str, str]:
    """Mesma query de painel_agentes/rascunhos_rota.carregar_nf_por_codigo_pedido."""
    if not codigos_base:
        return {}
    try:
        conn = _conectar()
        try:
            resultado: dict[str, set[str]] = {}
            lista = sorted(codigos_base)
            for i in range(0, len(lista), 900):
                lote = lista[i:i + 900]
                marc = ",".join("?" * len(lote))
                for row in conn.execute(
                        f"SELECT codigo_pedido, numero_nf FROM documentos_processados "
                        f"WHERE status='ENVIADO' AND tipo='Nota Fiscal' AND numero_nf IS NOT NULL "
                        f"AND codigo_pedido IN ({marc})", lote):
                    if row["numero_nf"]:
                        resultado.setdefault(row["codigo_pedido"], set()).add(str(row["numero_nf"]))
            return {c: ", ".join(sorted(v)) for c, v in resultado.items()}
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[portal] NF por código falhou: {e}")
        return {}


def _catalogo_motoristas(config: dict) -> dict:
    cfg = config.get("motoristas", {})
    try:
        catalogo = CatalogoMotoristas.carregar(cfg.get("planilha", ""), cfg.get("json_fallback", ""))
        return {m.agent_id: m for m in catalogo.motoristas if m.agent_id}
    except Exception as e:
        logger.warning(f"[portal] catálogo de motoristas indisponível: {e}")
        return {}


# ── Pedido (linha da tabela) ───────────────────────────────────────────────────

def _linha(servico: dict, situacao: str, nfs: dict[str, str], **extra) -> dict:
    cliente = _desembrulhar(servico, "customer")
    codigo = servico.get("code", "") or ""
    complemento = servico.get("address_complement") or ""
    endereco = servico.get("address") or ""
    return {
        "service_id": servico.get("id"),
        "sender_id": servico.get("sender_id"),
        "codigo": "#" + codigo.lstrip("#"),
        "nf": ", ".join(filter(None, (nfs.get(c, "") for c in codigos_base_lista(codigo)))),
        "destinatario": cliente.get("name") or (servico.get("title") or "")[:80],
        "endereco": _endereco_curto(endereco),
        "endereco_completo": (endereco + (" · " + complemento if complemento else "")).strip(),
        "telefone_destinatario": cliente.get("phone_number") or servico.get("phone_number") or "",
        "janela_atendimento": _janela(cliente),
        "volumes": _volumes(servico),
        "situacao": situacao,
        "situacao_rotulo": ROTULOS[situacao],
        "criado_em": (_parse_dt(servico.get("created_at")) or datetime.min).strftime("%d/%m/%Y %H:%M")
        if servico.get("created_at") else "",
        "observacoes": servico.get("note") or "",
        "agendado_para": None,
        "motorista": "", "rota": "", "placa": "", "ordem": None, "total_paradas": None,
        "detalhe": "", "concluido_em": "", "motivo": "", "comprovante": False,
        "reentrega": bool(re.search(r"-R\d+", codigo, re.IGNORECASE)),
        **extra,
    }


# ── Coleta ─────────────────────────────────────────────────────────────────────

def _remetentes(sender) -> tuple[int, ...]:
    """Um sender_id (login avulso) ou vários (login de grupo, 17/09), sempre
    como tupla ordenada -- serve de chave de cache e de filtro."""
    if isinstance(sender, (int, str)):
        return (int(sender),)
    return tuple(sorted({int(s) for s in sender}))


def buscar_rotas_do_dia(token: str, data_alvo: date) -> list[dict]:
    """Rotas da VUUPT com start_at no dia, com services/customer/agent. Separado
    de linhas_das_rotas pra quem percorre VARIOS embarcadores (e-mail diario
    notificar_nfs_em_rota.py) buscar na VUUPT uma vez so."""
    inicio = data_alvo.strftime("%Y-%m-%d") + " 00:00:00"
    fim = (data_alvo + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"
    filtro = [{"field": "start_at", "operator": "gte", "value": inicio},
              {"field": "start_at", "operator": "lt", "value": fim}]
    return listar_rotas(token, include=["services", "services.customer", "agent"], filtro=filtro)


def _coletar_rotas(token: str, sender_id, data_alvo: date, motoristas: dict, nfs_cache: dict) -> list[dict]:
    return linhas_das_rotas(buscar_rotas_do_dia(token, data_alvo), sender_id, motoristas, nfs_cache)


def linhas_das_rotas(rotas: list[dict], sender_id, motoristas: dict, nfs_cache: dict) -> list[dict]:
    """Pedidos do embarcador (ou das empresas do grupo) nas rotas dadas, com a
    situação parada a parada."""
    remetentes = _remetentes(sender_id)
    # NF de todos os pedidos do cliente nas rotas, numa query só.
    codigos = set()
    for rota in rotas:
        for s in _servicos_da_rota(rota):
            if s.get("sender_id") in remetentes:
                codigos.update(codigos_base_lista(s.get("code", "")))
    nfs_cache.update(nf_por_codigo(codigos))

    linhas = []
    for rota in rotas:
        if rota.get("status") == "canceled":
            continue
        validos = [s for s in _servicos_da_rota(rota) if s.get("status") != "canceled"]
        total = len(validos)
        agent_id = rota.get("agent_id")
        m = motoristas.get(agent_id)
        agente = _desembrulhar(rota, "agent")
        motorista = (m.nome if m else None) or agente.get("name") or ""
        placa = (m.placa if m else None) or ""
        saida = (rota.get("start_at") or "")[11:16]
        iniciada = _hora(rota.get("started_at"))
        # Próxima parada da rota = 1ª não finalizada (pra "parada N de M").
        entregues_rota = sum(1 for s in validos if s.get("status") == "done")

        for ordem, s in enumerate(validos, start=1):
            if s.get("sender_id") not in remetentes:
                continue
            status = s.get("status")
            if status == "done":
                if s.get("status_done") == "failed":
                    situacao, detalhe = "insucesso", texto_do_motivo(s.get("failed_reason_id"))
                else:
                    situacao, detalhe = "entregue", f"Entregue {_hora(s.get('completed_at'))}".strip()
            elif status in ("on_route", "arrived"):
                situacao = "em_rota"
                detalhe = f"Parada {ordem} de {total}" + (f" · {entregues_rota} já feitas" if entregues_rota else "")
            else:
                situacao = "programado"
                detalhe = f"Parada {ordem} de {total} · saída {saida}" if saida else f"Parada {ordem} de {total}"
            linhas.append(_linha(
                s, situacao, nfs_cache,
                motorista=motorista, rota=_rota_curta(rota.get("name"), rota.get("id")), placa=placa,
                ordem=ordem, total_paradas=total, detalhe=detalhe,
                concluido_em=_hora(s.get("completed_at")) if status == "done" else "",
                motivo=texto_do_motivo(s.get("failed_reason_id")) if situacao == "insucesso" else "",
                comprovante=(situacao == "entregue"),
                rota_iniciada_em=iniciada,
            ))
    return linhas


def _coletar_sem_rota(vuupt: VuuptClient, sender_id, data_alvo: date, nfs_cache: dict) -> tuple[list[dict], list[dict]]:
    """(linhas do dia sem rota, agendados futuros). Filtra por sender_id na
    API (uma consulta por empresa do grupo) e reconfere localmente (a API às
    vezes ignora filtro)."""
    servicos = []
    for remetente in _remetentes(sender_id):
        filtro = [{"field": "status", "operator": "eq", "value": "not_assigned"},
                  {"field": "sender_id", "operator": "eq", "value": remetente}]
        servicos += [s for s in vuupt.listar_servicos(filtro, per_page=100, include=["customer"])
                     if s.get("sender_id") == remetente]
    nfs_cache.update(nf_por_codigo({c for s in servicos for c in codigos_base_lista(s.get("code", ""))}))

    do_dia, futuros = [], []
    for s in servicos:
        agendada = _data_agendada(s)
        if agendada is None:
            do_dia.append(_linha(s, "aguardando_saida", nfs_cache, detalhe="Aguardando roteirização"))
        elif agendada <= data_alvo:
            do_dia.append(_linha(s, "aguardando_saida", nfs_cache,
                                 detalhe=f"Agendado {_ddmm(agendada)} · aguardando roteirização",
                                 agendado_para=agendada.isoformat()))
        else:
            hora = _hora(s.get("scheduled_start"))
            futuros.append(_linha(s, "agendado", nfs_cache,
                                  detalhe=f"Agendado {_dia_semana(agendada)} {_ddmm(agendada)}" + (f" · {hora}" if hora and hora != "00:00" else ""),
                                  agendado_para=agendada.isoformat()))
    return do_dia, futuros


def _coletar_atencao(sender_id) -> list[dict]:
    """Insucessos aguardando decisão do embarcador, agrupados por empresa +
    motivo (é assim que aplicar_decisao aplica a resposta: por grupo
    sender_id + failed_reason_id)."""
    remetentes = _remetentes(sender_id)
    try:
        conn = _conectar()
        try:
            rows = conn.execute(
                "SELECT service_id, code, sender_id, failed_reason_id, primeira_notificacao_em "
                f"FROM insucessos_aguardando_resposta WHERE sender_id IN ({','.join('?' * len(remetentes))}) "
                "AND status = 'PENDENTE' ORDER BY primeira_notificacao_em", remetentes).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.warning(f"[portal] insucessos_aguardando_resposta indisponível: {e}")
        return []
    grupos: dict[tuple, dict] = {}
    for r in rows:
        g = grupos.setdefault((r["sender_id"], r["failed_reason_id"]), {
            "sender_id": r["sender_id"],
            "failed_reason_id": r["failed_reason_id"],
            "motivo": texto_do_motivo(r["failed_reason_id"]),
            "pergunta": pergunta_do_motivo(r["failed_reason_id"]),
            "codigos": [], "service_ids": [], "desde": r["primeira_notificacao_em"],
        })
        codigo = "#" + (r["code"] or "").lstrip("#")
        if codigo not in g["codigos"]:  # reentrega repete o code do original
            g["codigos"].append(codigo)
        g["service_ids"].append(r["service_id"])
    return list(grupos.values())


def _historico(vuupt: VuuptClient, sender_id: int, hoje: date) -> dict | None:
    desde = (hoje - timedelta(days=HISTORICO_DIAS)).isoformat()
    filtro = [{"field": "status", "operator": "eq", "value": "done"},
              {"field": "sender_id", "operator": "eq", "value": sender_id},
              {"field": "completed_at", "operator": "gte", "value": desde}]
    try:
        servicos = [s for s in vuupt.listar_servicos(filtro, per_page=100, limite_paginas=20)
                    if s.get("sender_id") == sender_id and (s.get("completed_at") or "")[:10] >= desde]
    except Exception as e:
        logger.warning(f"[portal] histórico 30d indisponível: {e}")
        return None
    entregues = [s for s in servicos if s.get("status_done") != "failed"]
    insucessos = [s for s in servicos if s.get("status_done") == "failed"]
    primeira = sum(1 for s in entregues if not re.search(r"-R\d+", s.get("code") or "", re.IGNORECASE))
    return {"entregues": len(entregues), "insucessos": len(insucessos), "primeira": primeira}


def somar_historico(partes: list[dict | None]) -> dict | None:
    """Junta as contagens de cada empresa do grupo; o percentual de 1ª
    tentativa sai do total, não da média. Tudo indisponível -> None (o card
    some da tela)."""
    validas = [p for p in partes if p]
    if not validas:
        return None
    entregues = sum(p["entregues"] for p in validas)
    return {
        "dias": HISTORICO_DIAS,
        "entregues": entregues,
        "insucessos": sum(p["insucessos"] for p in validas),
        "primeira_tentativa_pct": round(100 * sum(p["primeira"] for p in validas) / entregues) if entregues else None,
    }


# ── Cache ──────────────────────────────────────────────────────────────────────

_cache_dia: dict[tuple, tuple[float, dict]] = {}
_cache_hist: dict[int, tuple[float, dict | None]] = {}
_lock = threading.Lock()


def montar_dia(sender_id, data_alvo: date, config: dict | None = None, forcar: bool = False) -> dict:
    """sender_id: um remetente (login avulso) ou a lista do grupo."""
    remetentes = _remetentes(sender_id)
    chave = (remetentes, data_alvo.isoformat())
    agora = time.time()
    with _lock:
        hit = _cache_dia.get(chave)
        if hit and not forcar and agora - hit[0] < CACHE_DIA_SEGUNDOS:
            return hit[1]
    dados = _montar_dia(remetentes, data_alvo, config or carregar_config())
    with _lock:
        _cache_dia[chave] = (time.time(), dados)
        # não deixa o cache crescer sem limite (datas antigas navegadas)
        if len(_cache_dia) > 200:
            for k in sorted(_cache_dia, key=lambda k: _cache_dia[k][0])[:100]:
                _cache_dia.pop(k, None)
    return dados


def invalidar_cache(sender_id: int) -> None:
    """Depois de uma ação do cliente (resposta a insucesso) o próximo
    carregamento busca de novo."""
    with _lock:
        for k in [k for k in _cache_dia if int(sender_id) in k[0]]:
            _cache_dia.pop(k, None)


def _montar_dia(sender_id: tuple[int, ...], data_alvo: date, config: dict) -> dict:
    token = config.get("vuupt_api", {}).get("token", "")
    vuupt = VuuptClient(token)
    hoje = date.today()
    motoristas = _catalogo_motoristas(config)
    nfs: dict[str, str] = {}

    linhas = _coletar_rotas(token, sender_id, data_alvo, motoristas, nfs)
    sem_rota, futuros = ([], [])
    if data_alvo >= hoje:
        # pedido sem rota é "do dia" só olhando pra frente -- num dia passado
        # a lista é o que de fato rodou.
        sem_rota, futuros = _coletar_sem_rota(vuupt, sender_id, data_alvo, nfs)
    pedidos = linhas + sem_rota

    ordem_situacao = {"insucesso": 0, "em_rota": 1, "programado": 2, "aguardando_saida": 3, "entregue": 4}
    pedidos.sort(key=lambda p: (ordem_situacao.get(p["situacao"], 9), p.get("rota") or "", p.get("ordem") or 0, p["codigo"]))

    contagem = defaultdict(int)
    for p in pedidos:
        contagem[p["situacao"]] += 1
    total = len(pedidos)
    volumes = sum(p["volumes"] for p in pedidos)
    rotas_em_andamento = {p["rota"] for p in pedidos if p["situacao"] == "em_rota"}

    atencao = _coletar_atencao(sender_id) if data_alvo >= hoje else []

    # próximos dias (agendados futuros)
    por_dia: dict[str, list] = defaultdict(list)
    for f in futuros:
        por_dia[f["agendado_para"]].append(f)
    proximos = [{"data": d, "rotulo": f"{_dia_semana(date.fromisoformat(d))} {_ddmm(date.fromisoformat(d))}",
                 "pedidos": len(v)} for d, v in sorted(por_dia.items())][:PROXIMOS_DIAS_MAX]

    # histórico 30 dias (cache 1 h por remetente; no grupo soma as empresas)
    partes = []
    for remetente in sender_id:
        with _lock:
            hit = _cache_hist.get(remetente)
        if hit and time.time() - hit[0] < CACHE_HISTORICO_SEGUNDOS:
            partes.append(hit[1])
            continue
        parte = _historico(vuupt, remetente, hoje)
        with _lock:
            _cache_hist[remetente] = (time.time(), parte)
        partes.append(parte)
    historico = somar_historico(partes)

    return {
        "data": data_alvo.isoformat(),
        "data_rotulo": f"{_dia_semana(data_alvo)} {data_alvo.strftime('%d/%m/%Y')}",
        "hoje": data_alvo == hoje,
        "atualizado_em": datetime.now().strftime("%H:%M"),
        "kpis": {
            "total": total, "volumes": volumes,
            "entregues": contagem["entregue"],
            "em_rota": contagem["em_rota"], "rotas_em_andamento": len(rotas_em_andamento),
            "programados": contagem["programado"],
            "aguardando_saida": contagem["aguardando_saida"],
            "insucessos": contagem["insucesso"],
            "aguardando_resposta": sum(len(g["codigos"]) for g in atencao),
        },
        "pedidos": pedidos,
        "atencao": atencao,
        "aguardando_saida": [p for p in pedidos if p["situacao"] in ("aguardando_saida", "programado")],
        "proximos_dias": proximos,
        "agendados_futuros": futuros,
        "historico": historico,
    }


# ── Canhoto ────────────────────────────────────────────────────────────────────

def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def buscar_servico(token: str, service_id: int) -> dict | None:
    """GET /services/{id} com checklist (comprovante) -- envelope
    {"service": ...}, {"data": ...} ou sem envelope."""
    try:
        resp = chamar_com_retry(requests.get, f"{API_BASE}/services/{service_id}",
                                headers=_headers(token), params={"include": "checklistAnswers"}, timeout=20)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        corpo = resp.json()
        for chave in ("service", "data"):
            if isinstance(corpo.get(chave), dict):
                return corpo[chave]
        return corpo
    except Exception as e:
        logger.warning(f"[portal] serviço {service_id} indisponível: {e}")
        return None


def checklist_id_do_servico(servico: dict) -> int | None:
    w = (servico or {}).get("checklistAnswers")
    lista = w.get("data") if isinstance(w, dict) else w
    if not lista:
        return None
    cl = lista[0] or {}
    if int(cl.get("images_quantity") or 0) <= 0:
        return None
    return cl.get("id")


def baixar_canhoto_pdf(token: str, checklist_id: int, codigo: str) -> Path | None:
    """Mesmo endpoint/nome de arquivo de expedir_pedidos.baixar_canhoto_pdf
    (dados/canhotos/canhoto_<codigo>.pdf) -- se a expedição já baixou,
    reaproveita."""
    CANHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    seguro = re.sub(r"[^A-Za-z0-9_-]", "_", (codigo or "").lstrip("#")) or str(checklist_id)
    destino = CANHOTOS_DIR / f"canhoto_{seguro}.pdf"
    if destino.exists() and destino.stat().st_size > 0:
        return destino
    try:
        resp = chamar_com_retry(requests.get, f"{API_BASE}/checklists/{checklist_id}/print",
                                headers={"Authorization": f"Bearer {token}", "Accept": "application/pdf"}, timeout=60)
        resp.raise_for_status()
        if not resp.content:
            return None
        destino.write_bytes(resp.content)
        return destino
    except Exception as e:
        logger.warning(f"[portal] canhoto checklist {checklist_id} falhou: {e}")
        return None
