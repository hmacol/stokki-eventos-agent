# -*- coding: utf-8 -*-
"""
portal_cliente/chamados.py

Atendimento do portal do cliente: chamados, mensagens, horário de
atendimento, e-mails do fluxo e leitor IMAP. Pedido do Hugo, 08-09/09/2026
(desenho aprovado no canvas f947025c):

- O cliente conversa num chat compacto dentro da tela de acompanhamento.
  Primeiro o ASSISTENTE (assistente.py, Claude) faz a triagem -- área,
  pedido/NF, resumo -- e tenta resolver sozinho; se não der, o chamado
  entra na fila da equipe (tela /atendimento do painel interno).
- Fora do horário (config portal_cliente.chamados.horario; seg-sex
  08:30-17:00, almoço 13:00-14:00) ou sem atendente online, o cliente
  DEIXA um chamado: vai por e-mail pro atendimento (entregas@) e a
  conversa segue por e-mail e pelo portal.
- Toda mensagem do cliente que não está num chat ao vivo vira e-mail pro
  atendimento; toda resposta da equipe que o cliente não está vendo ao
  vivo vira e-mail pro cliente. Assunto `[Chamado #N] ...` + marcador
  oculto [[CHAMADO:N]] ligam a resposta ao chamado certo.
- Ao resolver, o histórico completo vai por e-mail pro cliente.

Módulo de dados puro (sem Flask). As rotas ficam em chamados_web.py
(portal) e painel_agentes/atendimento.py (equipe).

COMO USAR (linha de comando):
    py -3 portal_cliente/chamados.py ler [--loop]      # leitor IMAP (produção: --loop no service)
    py -3 portal_cliente/chamados.py listar [--todos]
    py -3 portal_cliente/chamados.py responder <id> "texto"
    py -3 portal_cliente/chamados.py resolver <id>
"""
import argparse
import email
import html
import imaplib
import json
import logging
import re
import sqlite3
import sys
import threading
import time
from datetime import date, datetime, timedelta
from email.utils import getaddresses, make_msgid, parseaddr
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_AQUI))

import yaml

from email_leitura_utils import decodificar_header, extrair_texto_corpo, fetch_em_lote, remover_texto_citado
from email_utils import enviar_email, envelope_html

logger = logging.getLogger("portal_cliente.chamados")

DB_PATH = _RAIZ / "dados" / "dados.db"
PASTA_ANEXOS = _RAIZ / "dados" / "chamados"

# ── Constantes ─────────────────────────────────────────────────────────────────

STATUS_COM_ASSISTENTE = "COM_ASSISTENTE"
STATUS_NA_FILA = "NA_FILA"
STATUS_EM_ATENDIMENTO = "EM_ATENDIMENTO"
STATUS_AGUARDANDO_FL = "AGUARDANDO_FL"
STATUS_RESPONDIDO = "RESPONDIDO"
STATUS_RESOLVIDO = "RESOLVIDO"
STATUS_ABERTOS = (STATUS_COM_ASSISTENTE, STATUS_NA_FILA, STATUS_EM_ATENDIMENTO, STATUS_AGUARDANDO_FL, STATUS_RESPONDIDO)
ROTULOS_STATUS = {
    STATUS_COM_ASSISTENTE: "Com o assistente",
    STATUS_NA_FILA: "Na fila",
    STATUS_EM_ATENDIMENTO: "Em atendimento",
    STATUS_AGUARDANDO_FL: "Aguardando Fresh Log",
    STATUS_RESPONDIDO: "Respondido",
    STATUS_RESOLVIDO: "Resolvido",
}

ORIGEM_CLIENTE = "cliente"
ORIGEM_EQUIPE = "equipe"
ORIGEM_ASSISTENTE = "assistente"
ORIGEM_SISTEMA = "sistema"

CANAL_PORTAL = "portal"
CANAL_EMAIL = "email"

AREAS = {
    "entrega": "Entrega",
    "coleta": "Coleta / retirada",
    "avaria_falta": "Avaria ou falta",
    "comprovante": "Comprovante / canhoto",
    "financeiro": "Financeiro",
    "cadastro": "Cadastro / acesso",
    "outro": "Outro",
}

EXTENSOES_ANEXO = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf", ".xlsx", ".xls", ".csv", ".txt", ".xml"}
MAX_ANEXO_BYTES = 10 * 1024 * 1024
MAX_ANEXOS_POR_MENSAGEM = 5
MAX_TEXTO = 5000
ORIGEM_PROCESSADOS = "CHAMADO"          # namespace em emails_processados_respostas
CABECALHO_PORTAL = "X-FreshLog-Portal"  # marca os e-mails que NÓS mandamos (o leitor ignora)
RE_ASSUNTO_CHAMADO = re.compile(r"chamado\s*#\s*(\d+)", re.IGNORECASE)
RE_MARCADOR = re.compile(r"\[\[CHAMADO:(\d+)\]\]")
ATENDENTE_ONLINE_MINUTOS = 3     # heartbeat da tela do painel
CLIENTE_ONLINE_SEGUNDOS = 90     # última consulta do widget
DIAS_LISTAGEM_CLIENTE = 365
MESES = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


class ErroChamado(Exception):
    pass


# ── Config ─────────────────────────────────────────────────────────────────────

def carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def cfg_chamados(config: dict) -> dict:
    return (config.get("portal_cliente", {}) or {}).get("chamados", {}) or {}


_avisou_senha_vazia = False


def cfg_email_chamados(config: dict) -> dict:
    """Caixa que envia e RECEBE os e-mails dos chamados. Decisão do Hugo
    (09/09): entregas@freshlogbr.com. Enquanto a senha de app dessa caixa
    não estiver no config, cai na seção `email:` (hugo@)."""
    global _avisou_senha_vazia
    base = dict(config.get("email", {}) or {})
    propria = cfg_chamados(config).get("email", {}) or {}
    if propria.get("remetente") and propria.get("senha_app"):
        base.update({k: v for k, v in propria.items() if v})
    elif propria.get("remetente") and not _avisou_senha_vazia:
        _avisou_senha_vazia = True
        logger.warning("portal_cliente.chamados.email.senha_app vazio -- usando a caixa da seção email: (%s)",
                       base.get("remetente"))
    return base


def em_segundo_plano(fn, *args, **kwargs) -> None:
    """Roda `fn(conn, *args)` numa thread com conexão própria -- usado pros
    e-mails (SMTP leva uns 5 s cada; a resposta HTTP não espera)."""
    def alvo():
        conn = conectar()
        try:
            fn(conn, *args, **kwargs)
        except Exception:
            logger.exception("tarefa em segundo plano falhou: %s", getattr(fn, "__name__", fn))
        finally:
            conn.close()
    threading.Thread(target=alvo, daemon=True, name=f"chamados-{getattr(fn, '__name__', 'bg')}").start()


def emails_atendimento(config: dict) -> list[str]:
    c = cfg_chamados(config)
    lista = c.get("email_atendimento") or (config.get("email", {}) or {}).get("email_atendimento") \
        or (config.get("email", {}) or {}).get("email_responsavel")
    if isinstance(lista, str):
        lista = [e.strip() for e in re.split(r"[,;]", lista) if e.strip()]
    return list(lista or [])


def url_base(config: dict) -> str:
    return ((config.get("portal_cliente", {}) or {}).get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")


def url_painel(config: dict) -> str:
    return (cfg_chamados(config).get("url_painel") or "https://app.freshhub.com.br/painel").rstrip("/")


# ── Banco ──────────────────────────────────────────────────────────────────────

def conectar() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS portal_chamados (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            cnpj_embarcador     TEXT NOT NULL,
            sender_id           INTEGER,
            nome_cliente        TEXT,
            assunto             TEXT,
            area                TEXT,
            pedido_ref          TEXT,
            pedido_dados        TEXT,
            status              TEXT NOT NULL,
            origem              TEXT NOT NULL,
            etapa_assistente    TEXT,
            resumo_assistente   TEXT,
            atendente           TEXT,
            assumido_em         TEXT,
            atendente_visto_em  TEXT,
            cliente_visto_em    TEXT,
            ultima_origem       TEXT,
            ultima_msg_em       TEXT,
            fila_desde          TEXT,
            criado_em           TEXT NOT NULL,
            atualizado_em       TEXT NOT NULL,
            resolvido_em        TEXT,
            resolvido_por       TEXT,
            resolucao           TEXT,
            historico_enviado_em TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_portal_chamados_cnpj ON portal_chamados (cnpj_embarcador, status);
        CREATE INDEX IF NOT EXISTS idx_portal_chamados_status ON portal_chamados (status, atualizado_em);
        CREATE TABLE IF NOT EXISTS portal_chamados_mensagens (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            chamado_id       INTEGER NOT NULL,
            origem           TEXT NOT NULL,
            autor            TEXT,
            canal            TEXT NOT NULL,
            texto            TEXT NOT NULL,
            anexos           TEXT,
            opcoes           TEXT,
            message_id       TEXT,
            message_id_saida TEXT,
            email_remetente  TEXT,
            lido_cliente     INTEGER NOT NULL DEFAULT 0,
            lido_equipe      INTEGER NOT NULL DEFAULT 0,
            criado_em        TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_portal_chamados_msg ON portal_chamados_mensagens (chamado_id, id);
        CREATE TABLE IF NOT EXISTS portal_atendentes (
            usuario     TEXT PRIMARY KEY,
            nome        TEXT,
            status      TEXT NOT NULL,
            visto_em    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS emails_processados_respostas (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id       TEXT NOT NULL,
            origem           TEXT NOT NULL,
            remetente_email  TEXT,
            processado_em    TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(message_id, origem)
        );
    """)
    conn.commit()
    return conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dt(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _hora(valor: str | None) -> str:
    d = _dt(valor)
    return d.strftime("%H:%M") if d else ""


def rotulo_quando(valor: str | None, agora: datetime | None = None) -> str:
    """'Hoje 09:42' / 'Ontem 16:10' / '04/09' -- mesmo formato do desenho."""
    d = _dt(valor)
    if not d:
        return ""
    agora = agora or datetime.now()
    if d.date() == agora.date():
        return f"Hoje {d:%H:%M}"
    if d.date() == agora.date() - timedelta(days=1):
        return f"Ontem {d:%H:%M}"
    return d.strftime("%d/%m")


def _espera(valor: str | None) -> str:
    d = _dt(valor)
    if not d:
        return ""
    m = int((datetime.now() - d).total_seconds() // 60)
    if m < 1:
        return "agora"
    if m < 60:
        return f"há {m} min"
    if m < 24 * 60:
        return f"há {m // 60} h"
    return f"há {m // (24 * 60)} d"


# ── Horário de atendimento ─────────────────────────────────────────────────────

def _hm(txt, padrao: str) -> tuple[int, int]:
    try:
        h, m = str(txt or padrao).split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        h, m = padrao.split(":")
        return int(h), int(m)


def horario_config(config: dict) -> dict:
    h = cfg_chamados(config).get("horario", {}) or {}
    return {
        "dias": [int(d) for d in (h.get("dias") or [0, 1, 2, 3, 4])],   # 0 = segunda
        "inicio": _hm(h.get("inicio"), "08:30"),
        "fim": _hm(h.get("fim"), "17:00"),
        "almoco_inicio": _hm(h.get("almoco_inicio"), "13:00"),
        "almoco_fim": _hm(h.get("almoco_fim"), "14:00"),
    }


def _fmt_hm(hm: tuple[int, int]) -> str:
    h, m = hm
    return f"{h}h{m:02d}" if m else f"{h}h"


def texto_horario(config: dict) -> str:
    hc = horario_config(config)
    dias = hc["dias"]
    if dias == [0, 1, 2, 3, 4]:
        d = "seg a sex"
    else:
        d = ", ".join(DIAS_SEMANA[i] for i in dias)
    return (f"{d}, {_fmt_hm(hc['inicio'])} às {_fmt_hm(hc['fim'])} · almoço "
            f"{_fmt_hm(hc['almoco_inicio'])}–{_fmt_hm(hc['almoco_fim'])}")


def situacao_horario(config: dict, agora: datetime | None = None) -> dict:
    """{dentro: bool, motivo: None|'almoco'|'antes'|'depois'|'fora_dia', volta_em: str}."""
    agora = agora or datetime.now()
    hc = horario_config(config)
    minutos = agora.hour * 60 + agora.minute
    ini, fim = hc["inicio"][0] * 60 + hc["inicio"][1], hc["fim"][0] * 60 + hc["fim"][1]
    ai, af = hc["almoco_inicio"][0] * 60 + hc["almoco_inicio"][1], hc["almoco_fim"][0] * 60 + hc["almoco_fim"][1]

    def proximo_dia_util(d: date) -> date:
        for _ in range(8):
            d = d + timedelta(days=1)
            if d.weekday() in hc["dias"]:
                return d
        return d

    def rotulo_dia(d: date) -> str:
        if d == agora.date() + timedelta(days=1):
            return "amanhã"
        return DIAS_SEMANA[d.weekday()]

    if agora.weekday() not in hc["dias"]:
        prox = proximo_dia_util(agora.date())
        return {"dentro": False, "motivo": "fora_dia", "volta_em": f"{rotulo_dia(prox)} às {_fmt_hm(hc['inicio'])}"}
    if minutos < ini:
        return {"dentro": False, "motivo": "antes", "volta_em": f"hoje às {_fmt_hm(hc['inicio'])}"}
    if ai <= minutos < af:
        return {"dentro": False, "motivo": "almoco", "volta_em": f"às {_fmt_hm(hc['almoco_fim'])}"}
    if minutos >= fim:
        prox = proximo_dia_util(agora.date())
        return {"dentro": False, "motivo": "depois", "volta_em": f"{rotulo_dia(prox)} às {_fmt_hm(hc['inicio'])}"}
    return {"dentro": True, "motivo": None, "volta_em": ""}


# ── Atendentes (status + heartbeat) ────────────────────────────────────────────

def gravar_status_atendente(conn: sqlite3.Connection, usuario: str, nome: str, status: str) -> None:
    status = status if status in ("ONLINE", "ALMOCO", "OFFLINE") else "OFFLINE"
    conn.execute("""
        INSERT INTO portal_atendentes (usuario, nome, status, visto_em) VALUES (?, ?, ?, ?)
        ON CONFLICT(usuario) DO UPDATE SET nome = excluded.nome, status = excluded.status, visto_em = excluded.visto_em
    """, (usuario, nome, status, _agora()))
    conn.commit()


def status_atendente(conn: sqlite3.Connection, usuario: str) -> dict:
    r = conn.execute("SELECT * FROM portal_atendentes WHERE usuario = ?", (usuario,)).fetchone()
    return dict(r) if r else {"usuario": usuario, "nome": "", "status": "OFFLINE", "visto_em": None}


def atendentes_online(conn: sqlite3.Connection) -> list[dict]:
    limite = (datetime.now() - timedelta(minutes=ATENDENTE_ONLINE_MINUTOS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("SELECT * FROM portal_atendentes WHERE status = 'ONLINE' AND visto_em >= ?", (limite,)).fetchall()
    return [dict(r) for r in rows]


def situacao_atendimento(conn: sqlite3.Connection, config: dict, agora: datetime | None = None) -> dict:
    """O que o widget do cliente mostra no cabeçalho."""
    h = situacao_horario(config, agora)
    online = atendentes_online(conn)
    dentro = h["dentro"]
    if dentro and online:
        estado, texto = "online", "Online"
    elif dentro:
        estado, texto = "ausente", "Atendentes ocupados no momento"
    elif h["motivo"] == "almoco":
        estado, texto = "almoco", f"Em horário de almoço · voltamos {h['volta_em']}"
    else:
        estado, texto = "fechado", f"Fora do horário · voltamos {h['volta_em']}"
    return {"estado": estado, "texto": texto, "dentro_horario": dentro, "motivo": h["motivo"], "volta_em": h["volta_em"],
            "horario": texto_horario(config), "atendentes_online": len(online),
            "nomes_online": [a["nome"] or a["usuario"] for a in online]}


# ── Chamados ───────────────────────────────────────────────────────────────────

def _chamado_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["status_rotulo"] = ROTULOS_STATUS.get(d["status"], d["status"])
    d["area_rotulo"] = AREAS.get(d.get("area") or "", d.get("area") or "")
    d["quando"] = rotulo_quando(d.get("ultima_msg_em") or d.get("criado_em"))
    d["espera"] = _espera(d.get("fila_desde") or d.get("ultima_msg_em"))
    d["aberto"] = d["status"] in STATUS_ABERTOS
    try:
        d["pedido_dados"] = json.loads(d["pedido_dados"]) if d.get("pedido_dados") else None
    except (TypeError, ValueError):
        d["pedido_dados"] = None
    return d


def criar_chamado(conn: sqlite3.Connection, cliente: dict, origem: str, status: str, assunto: str = "",
                  area: str = "", pedido_ref: str = "", etapa_assistente: str | None = None) -> dict:
    agora = _agora()
    cur = conn.execute("""
        INSERT INTO portal_chamados (cnpj_embarcador, sender_id, nome_cliente, assunto, area, pedido_ref, status, origem,
                                     etapa_assistente, fila_desde, criado_em, atualizado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (cliente["cnpj"], cliente.get("sender_id"), cliente.get("nome"), assunto[:120], area, pedido_ref[:40],
          status, origem, etapa_assistente, agora if status in (STATUS_NA_FILA, STATUS_AGUARDANDO_FL) else None,
          agora, agora))
    conn.commit()
    return buscar_chamado(conn, cur.lastrowid)


def buscar_chamado(conn: sqlite3.Connection, chamado_id: int, cnpj: str | None = None) -> dict | None:
    sql = "SELECT * FROM portal_chamados WHERE id = ?"
    args: list = [chamado_id]
    if cnpj:
        sql += " AND cnpj_embarcador = ?"
        args.append(cnpj)
    r = conn.execute(sql, args).fetchone()
    return _chamado_dict(r) if r else None


def atualizar_chamado(conn: sqlite3.Connection, chamado_id: int, **campos) -> None:
    if not campos:
        return
    campos["atualizado_em"] = _agora()
    if "pedido_dados" in campos and campos["pedido_dados"] is not None and not isinstance(campos["pedido_dados"], str):
        campos["pedido_dados"] = json.dumps(campos["pedido_dados"], ensure_ascii=False)
    sets = ", ".join(f"{k} = ?" for k in campos)
    conn.execute(f"UPDATE portal_chamados SET {sets} WHERE id = ?", (*campos.values(), chamado_id))
    conn.commit()


def listar_chamados_cliente(conn: sqlite3.Connection, cnpj: str) -> list[dict]:
    limite = (datetime.now() - timedelta(days=DIAS_LISTAGEM_CLIENTE)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT c.*, (SELECT COUNT(*) FROM portal_chamados_mensagens m
                     WHERE m.chamado_id = c.id AND m.origem IN ('equipe','assistente','sistema') AND m.lido_cliente = 0) AS nao_lidas,
               (SELECT texto FROM portal_chamados_mensagens m WHERE m.chamado_id = c.id ORDER BY m.id DESC LIMIT 1) AS ultima_texto,
               (SELECT origem FROM portal_chamados_mensagens m WHERE m.chamado_id = c.id ORDER BY m.id DESC LIMIT 1) AS ultima_origem_msg
        FROM portal_chamados c
        WHERE c.cnpj_embarcador = ? AND c.criado_em >= ?
        ORDER BY (c.status = 'RESOLVIDO'), c.atualizado_em DESC
    """, (cnpj, limite)).fetchall()
    return [_chamado_dict(r) for r in rows]


def chamado_ativo_cliente(conn: sqlite3.Connection, cnpj: str) -> dict | None:
    """A conversa que o widget abre por padrão: o chamado aberto mais
    recente do cliente (chat ao vivo ou chamado aguardando)."""
    r = conn.execute("""
        SELECT * FROM portal_chamados WHERE cnpj_embarcador = ? AND status != 'RESOLVIDO'
        ORDER BY atualizado_em DESC LIMIT 1
    """, (cnpj,)).fetchone()
    return _chamado_dict(r) if r else None


def listar_fila(conn: sqlite3.Connection, aba: str = "fila", atendente: str | None = None, limite: int = 200) -> list[dict]:
    """Fila da tela interna. abas: fila (NA_FILA + AGUARDANDO_FL), meus
    (EM_ATENDIMENTO do atendente), email (AGUARDANDO_FL/RESPONDIDO com
    origem chamado/email), assistente (COM_ASSISTENTE), resolvidos, todos."""
    filtros = {
        "fila": "c.status IN ('NA_FILA', 'AGUARDANDO_FL')",
        "meus": "c.status = 'EM_ATENDIMENTO'" + (" AND c.atendente = ?" if atendente else ""),
        "email": "c.status IN ('AGUARDANDO_FL', 'RESPONDIDO') AND c.origem IN ('chamado', 'email')",
        "assistente": "c.status = 'COM_ASSISTENTE'",
        "resolvidos": "c.status = 'RESOLVIDO'",
        "abertos": "c.status != 'RESOLVIDO'",
        "todos": "1 = 1",
    }
    where = filtros.get(aba, filtros["fila"])
    args: list = [atendente] if (aba == "meus" and atendente) else []
    ordem = "c.resolvido_em DESC" if aba == "resolvidos" else \
        "CASE c.status WHEN 'NA_FILA' THEN 0 WHEN 'EM_ATENDIMENTO' THEN 1 WHEN 'AGUARDANDO_FL' THEN 2 WHEN 'COM_ASSISTENTE' THEN 3 ELSE 4 END, COALESCE(c.fila_desde, c.atualizado_em) ASC"
    rows = conn.execute(f"""
        SELECT c.*, (SELECT COUNT(*) FROM portal_chamados_mensagens m
                     WHERE m.chamado_id = c.id AND m.origem = 'cliente' AND m.lido_equipe = 0) AS nao_lidas,
               (SELECT texto FROM portal_chamados_mensagens m WHERE m.chamado_id = c.id AND m.origem != 'sistema' ORDER BY m.id DESC LIMIT 1) AS ultima_texto,
               (SELECT origem FROM portal_chamados_mensagens m WHERE m.chamado_id = c.id AND m.origem != 'sistema' ORDER BY m.id DESC LIMIT 1) AS ultima_origem_msg
        FROM portal_chamados c WHERE {where} ORDER BY {ordem} LIMIT ?
    """, (*args, limite)).fetchall()
    return [_chamado_dict(r) for r in rows]


def contagens_fila(conn: sqlite3.Connection, atendente: str | None = None) -> dict:
    hoje = date.today().strftime("%Y-%m-%d")
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    return {
        "fila": q("SELECT COUNT(*) FROM portal_chamados WHERE status IN ('NA_FILA','AGUARDANDO_FL')"),
        "na_fila": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'NA_FILA'"),
        "meus": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'EM_ATENDIMENTO' AND (? IS NULL OR atendente = ?)", atendente, atendente),
        "em_atendimento": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'EM_ATENDIMENTO'"),
        "email": q("SELECT COUNT(*) FROM portal_chamados WHERE status IN ('AGUARDANDO_FL','RESPONDIDO') AND origem IN ('chamado','email')"),
        "assistente": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'COM_ASSISTENTE'"),
        "resolvidos": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'RESOLVIDO'"),
        "resolvidos_hoje": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'RESOLVIDO' AND resolvido_em >= ?", hoje),
        "assistente_resolveu_hoje": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'RESOLVIDO' AND resolvido_por = 'assistente' AND resolvido_em >= ?", hoje),
        "nao_lidas_cliente": q("SELECT COUNT(*) FROM portal_chamados_mensagens m JOIN portal_chamados c ON c.id = m.chamado_id "
                               "WHERE m.origem = 'cliente' AND m.lido_equipe = 0 AND c.status != 'RESOLVIDO'"),
    }


def nao_lidas_cliente(conn: sqlite3.Connection, cnpj: str) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM portal_chamados_mensagens m JOIN portal_chamados c ON c.id = m.chamado_id
        WHERE c.cnpj_embarcador = ? AND m.origem IN ('equipe','assistente','sistema') AND m.lido_cliente = 0
    """, (cnpj,)).fetchone()[0]


# ── Mensagens ──────────────────────────────────────────────────────────────────

def _msg_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    for k in ("anexos", "opcoes"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else []
        except (TypeError, ValueError):
            d[k] = []
    d["hora"] = _hora(d.get("criado_em"))
    d["quando"] = rotulo_quando(d.get("criado_em"))
    return d


def mensagens(conn: sqlite3.Connection, chamado_id: int, desde_id: int = 0) -> list[dict]:
    rows = conn.execute("SELECT * FROM portal_chamados_mensagens WHERE chamado_id = ? AND id > ? ORDER BY id",
                        (chamado_id, desde_id)).fetchall()
    return [_msg_dict(r) for r in rows]


def adicionar_mensagem(conn: sqlite3.Connection, chamado: dict, origem: str, autor: str, texto: str,
                       canal: str = CANAL_PORTAL, anexos: list[dict] | None = None, opcoes: list[dict] | None = None,
                       message_id: str | None = None, email_remetente: str | None = None) -> dict:
    """Grava a mensagem e mantém o chamado coerente (última origem, status
    de reabertura, leitura de quem escreveu)."""
    texto = (texto or "").strip()[:MAX_TEXTO]
    if not texto and not anexos:
        raise ErroChamado("Escreva uma mensagem ou anexe um arquivo.")
    agora = _agora()
    lido_cliente = 1 if origem == ORIGEM_CLIENTE else 0
    lido_equipe = 1 if origem in (ORIGEM_EQUIPE, ORIGEM_SISTEMA, ORIGEM_ASSISTENTE) else 0
    cur = conn.execute("""
        INSERT INTO portal_chamados_mensagens (chamado_id, origem, autor, canal, texto, anexos, opcoes, message_id,
                                               email_remetente, lido_cliente, lido_equipe, criado_em)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (chamado["id"], origem, autor, canal, texto or "(anexo)", json.dumps(anexos or [], ensure_ascii=False),
          json.dumps(opcoes or [], ensure_ascii=False) if opcoes else None, message_id, email_remetente,
          lido_cliente, lido_equipe, agora))
    # opções (botões) de mensagens anteriores do assistente deixam de valer
    if origem in (ORIGEM_CLIENTE, ORIGEM_ASSISTENTE):
        conn.execute("UPDATE portal_chamados_mensagens SET opcoes = NULL WHERE chamado_id = ? AND id < ? AND opcoes IS NOT NULL",
                     (chamado["id"], cur.lastrowid))
    campos = {"ultima_msg_em": agora}
    if origem != ORIGEM_SISTEMA:
        campos["ultima_origem"] = origem
    conn.execute(f"UPDATE portal_chamados SET {', '.join(k + ' = ?' for k in campos)}, atualizado_em = ? WHERE id = ?",
                 (*campos.values(), agora, chamado["id"]))
    conn.commit()
    r = conn.execute("SELECT * FROM portal_chamados_mensagens WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _msg_dict(r)


def marcar_lidas(conn: sqlite3.Connection, chamado_id: int, por: str) -> None:
    if por == "cliente":
        conn.execute("UPDATE portal_chamados_mensagens SET lido_cliente = 1 WHERE chamado_id = ? AND lido_cliente = 0", (chamado_id,))
        conn.execute("UPDATE portal_chamados SET cliente_visto_em = ? WHERE id = ?", (_agora(), chamado_id))
    else:
        conn.execute("UPDATE portal_chamados_mensagens SET lido_equipe = 1 WHERE chamado_id = ? AND lido_equipe = 0", (chamado_id,))
        conn.execute("UPDATE portal_chamados SET atendente_visto_em = ? WHERE id = ?", (_agora(), chamado_id))
    conn.commit()


def cliente_esta_online(chamado: dict) -> bool:
    d = _dt(chamado.get("cliente_visto_em"))
    return bool(d and (datetime.now() - d).total_seconds() <= CLIENTE_ONLINE_SEGUNDOS)


def atendente_esta_online(chamado: dict) -> bool:
    d = _dt(chamado.get("atendente_visto_em"))
    return bool(d and (datetime.now() - d).total_seconds() <= ATENDENTE_ONLINE_MINUTOS * 60)


# ── Anexos ─────────────────────────────────────────────────────────────────────

def _nome_seguro(nome: str) -> str:
    nome = re.sub(r"[^\w.\-]+", "_", (nome or "arquivo").strip(), flags=re.UNICODE)[:80]
    return nome or "arquivo"


def guardar_anexos(chamado_id: int, arquivos: list[tuple[str, bytes]]) -> list[dict]:
    """arquivos: [(nome_original, bytes)]. Valida extensão/tamanho e grava em
    dados/chamados/<id>/. Devolve a lista pra coluna `anexos`."""
    if len(arquivos) > MAX_ANEXOS_POR_MENSAGEM:
        raise ErroChamado(f"No máximo {MAX_ANEXOS_POR_MENSAGEM} arquivos por mensagem.")
    pasta = PASTA_ANEXOS / str(chamado_id)
    pasta.mkdir(parents=True, exist_ok=True)
    saida = []
    for nome, conteudo in arquivos:
        ext = Path(nome or "").suffix.lower()
        if ext not in EXTENSOES_ANEXO:
            raise ErroChamado(f"Tipo de arquivo não aceito: {nome}. Envie foto (JPG/PNG), PDF ou planilha.")
        if len(conteudo) > MAX_ANEXO_BYTES:
            raise ErroChamado(f"{nome} passa de 10 MB.")
        if not conteudo:
            continue
        base = _nome_seguro(Path(nome).stem)
        arquivo = f"{datetime.now():%Y%m%d%H%M%S}_{base}{ext}"
        (pasta / arquivo).write_bytes(conteudo)
        saida.append({"nome": nome, "arquivo": arquivo, "tamanho": len(conteudo), "tipo": ext.lstrip(".")})
    return saida


def caminho_anexo(chamado_id: int, arquivo: str) -> Path | None:
    if not arquivo or "/" in arquivo or "\\" in arquivo or arquivo.startswith("."):
        return None
    p = PASTA_ANEXOS / str(chamado_id) / arquivo
    return p if p.is_file() else None


def tamanho_legivel(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1024 / 1024:.1f} MB".replace(".", ",")


# ── Transições ─────────────────────────────────────────────────────────────────

def mensagem_sistema(conn, chamado: dict, texto: str) -> dict:
    return adicionar_mensagem(conn, chamado, ORIGEM_SISTEMA, "", texto)


def entrar_na_fila(conn: sqlite3.Connection, chamado: dict, config: dict) -> dict:
    """Cliente pediu atendente (ou o assistente encaminhou). Online -> NA_FILA;
    fora do horário / sem atendente -> AGUARDANDO_FL (vira chamado por e-mail)."""
    sit = situacao_atendimento(conn, config)
    if sit["estado"] == "online":
        posicao = conn.execute("SELECT COUNT(*) FROM portal_chamados WHERE status = 'NA_FILA'").fetchone()[0] + 1
        atualizar_chamado(conn, chamado["id"], status=STATUS_NA_FILA, fila_desde=_agora())
        nomes = ", ".join(sit["nomes_online"][:2])
        mensagem_sistema(conn, chamado, f"Você está na fila ({posicao}º). {nomes or 'A equipe'} vai assumir em instantes.")
        return {"status": STATUS_NA_FILA, "posicao": posicao, "online": True}
    atualizar_chamado(conn, chamado["id"], status=STATUS_AGUARDANDO_FL, fila_desde=_agora())
    if sit["estado"] == "ausente":
        txt = "Nossos atendentes estão ocupados agora. Deixamos seu chamado registrado: a equipe responde aqui e no seu e-mail assim que possível."
    else:
        txt = (f"{sit['texto']}. Deixamos seu chamado registrado: a equipe responde aqui e no seu e-mail "
               f"quando voltar ({texto_horario(config)}).")
    mensagem_sistema(conn, chamado, txt)
    return {"status": STATUS_AGUARDANDO_FL, "online": False}


def assumir(conn: sqlite3.Connection, chamado: dict, usuario: str, nome: str) -> dict:
    if chamado["status"] == STATUS_RESOLVIDO:
        raise ErroChamado("Chamado já resolvido -- reabra antes de assumir.")
    atualizar_chamado(conn, chamado["id"], status=STATUS_EM_ATENDIMENTO, atendente=usuario, assumido_em=_agora(),
                      atendente_visto_em=_agora(), fila_desde=None)
    mensagem_sistema(conn, chamado, f"{nome or usuario} entrou na conversa")
    return buscar_chamado(conn, chamado["id"])


def transferir(conn: sqlite3.Connection, chamado: dict, de_nome: str) -> dict:
    atualizar_chamado(conn, chamado["id"], status=STATUS_NA_FILA, atendente=None, assumido_em=None, fila_desde=_agora())
    mensagem_sistema(conn, chamado, f"{de_nome} devolveu o chamado pra fila")
    return buscar_chamado(conn, chamado["id"])


def resolver(conn: sqlite3.Connection, chamado: dict, por: str, resolucao: str = "") -> dict:
    if chamado["status"] == STATUS_RESOLVIDO:
        return chamado
    atualizar_chamado(conn, chamado["id"], status=STATUS_RESOLVIDO, resolvido_em=_agora(), resolvido_por=por,
                      resolucao=(resolucao or "")[:1000], fila_desde=None)
    quem = {"cliente": "por você", "assistente": "pelo assistente"}.get(por, f"por {por}")
    mensagem_sistema(conn, chamado, f"Chamado marcado como resolvido {quem}")
    return buscar_chamado(conn, chamado["id"])


def reabrir(conn: sqlite3.Connection, chamado: dict, config: dict, motivo: str = "") -> dict:
    """Nova mensagem num chamado resolvido reabre; vai pra fila se online,
    senão aguarda por e-mail."""
    sit = situacao_atendimento(conn, config)
    novo = STATUS_NA_FILA if sit["estado"] == "online" else STATUS_AGUARDANDO_FL
    atualizar_chamado(conn, chamado["id"], status=novo, resolvido_em=None, resolvido_por=None, fila_desde=_agora(),
                      historico_enviado_em=None)
    mensagem_sistema(conn, chamado, "Chamado reaberto" + (f" ({motivo})" if motivo else ""))
    return buscar_chamado(conn, chamado["id"])


def registrar_mensagem_cliente(conn: sqlite3.Connection, chamado: dict, cliente: dict, texto: str, anexos: list[dict],
                               config: dict, canal: str = CANAL_PORTAL, message_id: str | None = None,
                               email_remetente: str | None = None) -> tuple[dict, dict, bool]:
    """Mensagem do cliente num chamado que NÃO está com o assistente.
    Devolve (mensagem, chamado_atualizado, precisa_avisar_equipe_por_email)."""
    if chamado["status"] == STATUS_RESOLVIDO:
        chamado = reabrir(conn, chamado, config, "nova mensagem do cliente")
    msg = adicionar_mensagem(conn, chamado, ORIGEM_CLIENTE, cliente.get("nome") or "Cliente", texto, canal, anexos,
                             message_id=message_id, email_remetente=email_remetente)
    chamado = buscar_chamado(conn, chamado["id"])
    if chamado["status"] == STATUS_RESPONDIDO:
        atualizar_chamado(conn, chamado["id"], status=STATUS_AGUARDANDO_FL, fila_desde=_agora())
        chamado = buscar_chamado(conn, chamado["id"])
    # e-mail pro atendimento se ninguém está olhando ao vivo
    avisar = not (chamado["status"] == STATUS_EM_ATENDIMENTO and atendente_esta_online(chamado)) \
        and chamado["status"] != STATUS_NA_FILA
    return msg, chamado, avisar


def registrar_mensagem_equipe(conn: sqlite3.Connection, chamado: dict, usuario: str, nome: str, texto: str,
                              anexos: list[dict], config: dict, canal: str = CANAL_PORTAL,
                              message_id: str | None = None, email_remetente: str | None = None) -> tuple[dict, dict, bool]:
    """Resposta da equipe. Devolve (mensagem, chamado, precisa_mandar_email_pro_cliente)."""
    if chamado["status"] == STATUS_RESOLVIDO:
        chamado = reabrir(conn, chamado, config, "resposta da equipe")
    msg = adicionar_mensagem(conn, chamado, ORIGEM_EQUIPE, nome or usuario, texto, canal, anexos,
                             message_id=message_id, email_remetente=email_remetente)
    campos = {}
    if chamado["status"] in (STATUS_AGUARDANDO_FL, STATUS_NA_FILA, STATUS_COM_ASSISTENTE):
        campos["status"] = STATUS_RESPONDIDO
        campos["fila_desde"] = None
    if canal == CANAL_PORTAL:
        campos["atendente_visto_em"] = _agora()
        if not chamado.get("atendente"):
            campos["atendente"] = usuario
    if campos:
        atualizar_chamado(conn, chamado["id"], **campos)
    chamado = buscar_chamado(conn, chamado["id"])
    # cliente vendo ao vivo no chat? então não precisa de e-mail
    ao_vivo = chamado["status"] == STATUS_EM_ATENDIMENTO and cliente_esta_online(chamado)
    return msg, chamado, not ao_vivo


# ── E-mails ────────────────────────────────────────────────────────────────────

def _assunto_email(chamado: dict, prefixo: str = "") -> str:
    base = f"[Chamado #{chamado['id']}] {chamado.get('assunto') or 'Atendimento Fresh Log'}"
    return (prefixo + base) if prefixo else base


def _cabecalhos_thread(conn: sqlite3.Connection, chamado: dict, message_id_novo: str, config: dict) -> dict:
    rows = conn.execute("""
        SELECT message_id, message_id_saida FROM portal_chamados_mensagens
        WHERE chamado_id = ? AND (message_id IS NOT NULL OR message_id_saida IS NOT NULL) ORDER BY id
    """, (chamado["id"],)).fetchall()
    ids = []
    for r in rows:
        for k in ("message_id", "message_id_saida"):
            if r[k] and r[k] not in ids:
                ids.append(r[k])
    cab = {"Message-ID": message_id_novo, CABECALHO_PORTAL: f"chamado {chamado['id']}",
           "Reply-To": cfg_email_chamados(config).get("remetente", "")}
    if ids:
        cab["In-Reply-To"] = ids[-1]
        cab["References"] = " ".join(ids[-10:])
    return cab


def _html_texto(texto: str) -> str:
    return html.escape(texto or "").replace("\n", "<br>")


def _bloco_mensagem(msg: dict, cor: str) -> str:
    quem = {"cliente": msg.get("autor") or "Cliente", "equipe": (msg.get("autor") or "Fresh Log") + " · Fresh Log",
            "assistente": "Assistente Fresh Log", "sistema": "Sistema"}.get(msg["origem"], msg.get("autor") or "")
    anexos = ""
    if msg.get("anexos"):
        anexos = "<div style='margin-top:6px;font-size:12px;color:#6B7280'>Anexos: " + ", ".join(
            html.escape(a["nome"]) for a in msg["anexos"]) + "</div>"
    return (f"<div style='border-left:3px solid {cor};background:#F9FAFB;padding:10px 14px;border-radius:0 8px 8px 0;font-size:13.5px'>"
            f"{_html_texto(msg['texto'])}{anexos}"
            f"<div style='margin-top:6px;font-size:12px;color:#6B7280'>{html.escape(quem)} · {msg.get('quando', '')}</div></div>")


def _rodape(chamado: dict, config: dict) -> str:
    return (f"Fresh Log · Portal do cliente · chamado #{chamado['id']} · {url_base(config)}<br>"
            f"<span style='color:#9CA3AF'>Mantenha o número do chamado no assunto ao responder. "
            f"<span style='font-family:Consolas,monospace'>[[CHAMADO:{chamado['id']}]]</span></span>")


def _anexos_email(chamado_id: int, msg: dict) -> list[tuple[Path, str]]:
    saida = []
    for a in msg.get("anexos") or []:
        p = caminho_anexo(chamado_id, a["arquivo"])
        if p:
            saida.append((p, a["nome"]))
    return saida


def _mandar(conn, chamado: dict, msg: dict | None, destinatarios: list[str], assunto: str, html_corpo: str,
            config: dict, anexos=None) -> bool:
    if not destinatarios:
        logger.warning("chamado %s: sem destinatário de e-mail", chamado["id"])
        return False
    forcar = cfg_chamados(config).get("forcar_destino")
    if forcar:
        # piloto/teste: TODO e-mail dos chamados vai só pra este endereço
        # (mesmo padrão do forcar_destino das notificações de área)
        logger.info("chamado %s: forcar_destino ativo, %s -> %s", chamado["id"], destinatarios, forcar)
        destinatarios = [forcar] if isinstance(forcar, str) else list(forcar)
    dominio = (cfg_email_chamados(config).get("remetente") or "freshlogbr.com").split("@")[-1]
    mid = make_msgid(domain=dominio)
    cab = _cabecalhos_thread(conn, chamado, mid, config)
    ok = enviar_email(destinatarios, assunto, html_corpo, cfg_email_chamados(config), anexos=anexos, cabecalhos_extra=cab)
    if ok and msg:
        conn.execute("UPDATE portal_chamados_mensagens SET message_id_saida = ? WHERE id = ?", (mid, msg["id"]))
        conn.commit()
    return ok


def email_para_atendimento(conn, chamado: dict, msg: dict, config: dict, novo: bool = False) -> bool:
    """Aviso pro atendimento (entregas@): chamado novo deixado fora do
    horário, ou mensagem do cliente num chamado que ninguém está vendo."""
    titulo = ("deixou o chamado" if novo else "mandou uma mensagem no chamado")
    ctx = "" if not chamado.get("resumo_assistente") else \
        f"<tr><td style='padding:6px 0;color:#6B7280;vertical-align:top'>Triagem do assistente</td><td style='padding:6px 0'>{_html_texto(chamado['resumo_assistente'])}</td></tr>"
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'><b>{html.escape(chamado.get('nome_cliente') or chamado['cnpj_embarcador'])}</b> {titulo} "
        f"<b>#{chamado['id']}</b> pelo {'e-mail' if msg.get('canal') == CANAL_EMAIL else 'portal'}"
        f"{' <b>fora do horário de atendimento</b>' if novo and chamado.get('origem') == 'chamado' else ''}.</p>"
        f"<table style='border-collapse:collapse;font-size:13px;width:100%'>"
        f"<tr><td style='padding:6px 0;color:#6B7280;width:150px'>Assunto</td><td style='padding:6px 0;font-weight:600'>{html.escape(chamado.get('assunto') or '(sem assunto)')}</td></tr>"
        f"<tr><td style='padding:6px 0;color:#6B7280'>Área</td><td style='padding:6px 0'>{html.escape(chamado.get('area_rotulo') or '-')}</td></tr>"
        f"<tr><td style='padding:6px 0;color:#6B7280'>Pedido / NF</td><td style='padding:6px 0;font-family:Consolas,monospace'>{html.escape(chamado.get('pedido_ref') or '-')}</td></tr>"
        f"<tr><td style='padding:6px 0;color:#6B7280'>Situação</td><td style='padding:6px 0'>{chamado['status_rotulo']}</td></tr>{ctx}</table>"
        f"<div style='margin:14px 0'>{_bloco_mensagem(msg, '#F5A623')}</div>"
        f"<p style='margin:8px 0 2px'><a href='{url_painel(config)}/atendimento?chamado={chamado['id']}' style='display:inline-block;background:#141428;color:#fff;padding:12px 22px;border-radius:7px;text-decoration:none;font-weight:700'>Abrir na tela de Atendimento</a></p>"
        f"<div style='background:#E6FBF5;border-radius:8px;padding:12px 16px;font-size:13px;margin-top:14px'><b>Pra responder ao cliente, é só responder este e-mail.</b> "
        f"O texto vai pro cliente e entra no chamado em alguns minutos. Anexos também vão. Ou responda pela tela de Atendimento do painel.</div>",
        rodape=_rodape(chamado, config), cor_acento="#F5A623")
    return _mandar(conn, chamado, msg, emails_atendimento(config), _assunto_email(chamado) + f" · {chamado.get('nome_cliente') or ''}".rstrip(" ·"),
                   corpo, config, anexos=_anexos_email(chamado["id"], msg))


def email_confirmacao_cliente(conn, chamado: dict, msg: dict, emails: list[str], config: dict) -> bool:
    """Cliente deixou um chamado fora do horário: confirma por e-mail (e
    assim ele já pode responder por e-mail também)."""
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'>Olá, <b>{html.escape(chamado.get('nome_cliente') or '')}</b>. Recebemos seu chamado "
        f"<b>#{chamado['id']} · {html.escape(chamado.get('assunto') or '')}</b>.</p>"
        f"<div style='margin:14px 0'>{_bloco_mensagem(msg, '#2A78D6')}</div>"
        f"<p style='margin:0 0 12px;font-size:13.5px'>A equipe Fresh Log responde aqui por e-mail e no portal. Nosso horário: {html.escape(texto_horario(config))}. "
        f"Pra complementar, é só <b>responder este e-mail</b>.</p>"
        f"<p style='margin:8px 0 2px'><a href='{url_base(config)}/?chamado={chamado['id']}' style='display:inline-block;background:#0EA575;color:#fff;padding:12px 22px;border-radius:7px;text-decoration:none;font-weight:700'>Ver o chamado no portal</a></p>",
        rodape=_rodape(chamado, config))
    return _mandar(conn, chamado, None, emails, _assunto_email(chamado), corpo, config)


def email_resposta_cliente(conn, chamado: dict, msg: dict, emails: list[str], config: dict) -> bool:
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'>Olá, <b>{html.escape(chamado.get('nome_cliente') or '')}</b>. A Fresh Log respondeu ao seu chamado "
        f"<b>#{chamado['id']} · {html.escape(chamado.get('assunto') or '')}</b>:</p>"
        f"<div style='margin:14px 0'>{_bloco_mensagem(msg, '#00C896')}</div>"
        f"<p style='margin:0 0 12px;font-size:13.5px'>Pra continuar a conversa, <b>responda este e-mail</b> ou abra o chamado no portal. Fotos e PDF anexados também entram no chamado.</p>"
        f"<p style='margin:8px 0 2px'><a href='{url_base(config)}/?chamado={chamado['id']}' style='display:inline-block;background:#0EA575;color:#fff;padding:12px 22px;border-radius:7px;text-decoration:none;font-weight:700'>Ver o chamado no portal</a></p>"
        f"<p style='margin:12px 0 0;font-size:12.5px;color:#6B7280'>Quando o assunto estiver resolvido, marque o chamado como resolvido no portal. Uma nova mensagem reabre o chamado.</p>",
        rodape=_rodape(chamado, config))
    return _mandar(conn, chamado, msg, emails, _assunto_email(chamado, "Re: "), corpo, config,
                   anexos=_anexos_email(chamado["id"], msg))


def email_historico(conn, chamado: dict, emails: list[str], config: dict) -> bool:
    """Ao resolver: dados do chamado + a conversa inteira (pedido do Hugo, 09/09)."""
    msgs = mensagens(conn, chamado["id"])
    cores = {"cliente": "#141428", "equipe": "#0EA575", "assistente": "#2A78D6", "sistema": "#9CA3AF"}
    nomes = {"cliente": chamado.get("nome_cliente") or "Cliente", "assistente": "Assistente", "sistema": "Sistema"}
    linhas = []
    for m in msgs:
        quem = nomes.get(m["origem"]) or ((m.get("autor") or "Fresh Log") + " · Fresh Log")
        if m["origem"] == "equipe":
            quem = (m.get("autor") or "Fresh Log") + " · Fresh Log"
        anexos = (" <i style='color:#6B7280'>(anexo: " + ", ".join(html.escape(a["nome"]) for a in m["anexos"]) + ")</i>") if m.get("anexos") else ""
        linhas.append(f"<div style='display:flex;gap:10px;padding:8px 0;border-top:1px solid #E5E7EB;font-size:13px'>"
                      f"<span style='width:100px;flex:none;color:#6B7280;font-family:Consolas,monospace;font-size:12px'>{m['quando']}</span>"
                      f"<div><b style='color:{cores.get(m['origem'], '#1F2937')}'>{html.escape(quem)}</b><span style='color:#6B7280'> — </span>{_html_texto(m['texto'])}{anexos}</div></div>")
    ini, fim = _dt(chamado.get("criado_em")), _dt(chamado.get("resolvido_em"))
    duracao = ""
    if ini and fim:
        mins = int((fim - ini).total_seconds() // 60)
        duracao = f" · {mins} min" if mins < 120 else f" · {mins // 60} h"
    quem_resolveu = {"cliente": "você", "assistente": "o assistente"}.get(chamado.get("resolvido_por") or "", chamado.get("resolvido_por") or "")
    if quem_resolveu and quem_resolveu not in ("você", "o assistente"):
        quem_resolveu += " · Fresh Log"
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'>Olá, <b>{html.escape(chamado.get('nome_cliente') or '')}</b>. Segue o histórico do atendimento que acabamos de encerrar.</p>"
        f"<table style='border-collapse:collapse;font-size:13px;width:100%;background:#F9FAFB;border-radius:8px'>"
        f"<tr><td style='padding:7px 12px;color:#6B7280;width:150px'>Chamado</td><td style='padding:7px 12px;font-weight:600'>#{chamado['id']} · {html.escape(chamado.get('assunto') or '')}</td></tr>"
        f"<tr><td style='padding:7px 12px;color:#6B7280'>Área</td><td style='padding:7px 12px'>{html.escape(chamado.get('area_rotulo') or '-')}</td></tr>"
        f"<tr><td style='padding:7px 12px;color:#6B7280'>Pedido / NF</td><td style='padding:7px 12px;font-family:Consolas,monospace'>{html.escape(chamado.get('pedido_ref') or '-')}</td></tr>"
        f"<tr><td style='padding:7px 12px;color:#6B7280'>Aberto</td><td style='padding:7px 12px'>{ini.strftime('%d/%m às %H:%M') if ini else ''} pelo {'chat do portal' if chamado.get('origem') == 'chat' else 'portal'}</td></tr>"
        f"<tr><td style='padding:7px 12px;color:#6B7280'>Encerrado</td><td style='padding:7px 12px'>{fim.strftime('%d/%m às %H:%M') if fim else ''}{(' por ' + html.escape(quem_resolveu)) if quem_resolveu else ''}{duracao}</td></tr>"
        + (f"<tr><td style='padding:7px 12px;color:#6B7280'>Resolução</td><td style='padding:7px 12px'>{_html_texto(chamado['resolucao'])}</td></tr>" if chamado.get("resolucao") else "")
        + f"</table><div style='font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.4px;color:#6B7280;margin:16px 0 4px'>Conversa</div>"
        f"<div style='border-bottom:1px solid #E5E7EB'>{''.join(linhas)}</div>"
        f"<p style='margin:14px 0 0;font-size:13px'>Ficou algo pendente? <b>Responda este e-mail</b> e o chamado #{chamado['id']} reabre automaticamente — ou fale com a gente pelo chat do portal.</p>"
        f"<p style='margin:12px 0 2px'><a href='{url_base(config)}/?chamado={chamado['id']}' style='display:inline-block;background:#0EA575;color:#fff;padding:11px 20px;border-radius:7px;text-decoration:none;font-weight:700'>Ver o chamado no portal</a></p>",
        rodape=_rodape(chamado, config))
    ok = _mandar(conn, chamado, None, emails, f"[Chamado #{chamado['id']}] Histórico do atendimento · {chamado.get('assunto') or ''}".rstrip(" ·"),
                 corpo, config)
    if ok:
        atualizar_chamado(conn, chamado["id"], historico_enviado_em=_agora())
    return ok


def emails_do_cliente(conn: sqlite3.Connection, cnpj: str) -> list[str]:
    import auth_cliente
    emb = auth_cliente.buscar_embarcador(conn, cnpj)
    return list(emb["emails"]) if emb else []


# ── Leitor IMAP ────────────────────────────────────────────────────────────────

def _data_imap(d: date) -> str:
    return f"{d.day:02d}-{['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][d.month - 1]}-{d.year}"


def _ja_processado(conn, message_id: str) -> bool:
    if not message_id:
        return False
    return conn.execute("SELECT 1 FROM emails_processados_respostas WHERE message_id = ? AND origem = ?",
                        (message_id, ORIGEM_PROCESSADOS)).fetchone() is not None


def _marcar_processado(conn, message_id: str, remetente: str) -> None:
    if message_id:
        conn.execute("INSERT OR IGNORE INTO emails_processados_respostas (message_id, origem, remetente_email) VALUES (?, ?, ?)",
                     (message_id, ORIGEM_PROCESSADOS, remetente))
        conn.commit()


def _numero_chamado(assunto: str, corpo: str) -> int | None:
    m = RE_ASSUNTO_CHAMADO.search(assunto or "") or RE_MARCADOR.search(corpo or "")
    return int(m.group(1)) if m else None


def _classificar_remetente(conn, chamado: dict, remetente: str, config: dict) -> str | None:
    """'cliente' se o e-mail é do cliente do chamado; 'equipe' se é da Fresh
    Log; None = desconhecido (ignora, por segurança)."""
    remetente = (remetente or "").lower()
    if not remetente:
        return None
    emails_cli = [e.lower() for e in emails_do_cliente(conn, chamado["cnpj_embarcador"])]
    if remetente in emails_cli:
        return ORIGEM_CLIENTE
    dominio = remetente.split("@")[-1]
    internos = {e.lower() for e in emails_atendimento(config)}
    internos.add((cfg_email_chamados(config).get("remetente") or "").lower())
    internos.add(((config.get("email", {}) or {}).get("remetente") or "").lower())
    dominios_equipe = {d.lower() for d in (cfg_chamados(config).get("dominios_equipe") or ["freshlogbr.com", "freshhub.com.br"])}
    if remetente in internos or dominio in dominios_equipe:
        return ORIGEM_EQUIPE
    if dominio in {e.split("@")[-1] for e in emails_cli}:
        return ORIGEM_CLIENTE
    return None


def _anexos_do_email(msg, chamado_id: int) -> list[dict]:
    arquivos = []
    for parte in msg.walk():
        if parte.get_content_maintype() == "multipart":
            continue
        nome = parte.get_filename()
        if not nome:
            continue
        nome = decodificar_header(nome)
        if Path(nome).suffix.lower() not in EXTENSOES_ANEXO:
            continue
        try:
            conteudo = parte.get_payload(decode=True) or b""
        except Exception:
            continue
        if 0 < len(conteudo) <= MAX_ANEXO_BYTES and len(arquivos) < MAX_ANEXOS_POR_MENSAGEM:
            arquivos.append((nome, conteudo))
    return guardar_anexos(chamado_id, arquivos) if arquivos else []


def _limpar_texto_email(corpo: str) -> str:
    texto = remover_texto_citado(corpo or "")
    texto = RE_MARCADOR.sub("", texto)
    texto = re.sub(r"\n{3,}", "\n\n", texto).strip()
    return texto[:MAX_TEXTO]


def processar_emails(config: dict | None = None, dias: int | None = None) -> dict:
    """Lê a caixa dos chamados (IMAP), traz respostas pra dentro dos
    chamados e reencaminha pro outro lado quando preciso."""
    config = config or carregar_config()
    cfg = cfg_email_chamados(config)
    usuario, senha = cfg.get("remetente", ""), cfg.get("senha_app") or cfg.get("senha", "")
    if not usuario or not senha:
        logger.warning("leitor de chamados desativado: remetente/senha_app ausentes")
        return {"lidos": 0, "gravados": 0, "ignorados": 0}
    dias = dias or int(cfg_chamados(config).get("dias_buscar_respostas") or 7)
    host, porta = cfg.get("imap_host", "imap.gmail.com"), int(cfg.get("imap_port", 993))
    resultado = {"lidos": 0, "gravados": 0, "ignorados": 0}
    conn = conectar()
    try:
        mail = imaplib.IMAP4_SSL(host, porta, timeout=30)
        mail.login(usuario, senha)
        mail.select("INBOX")
        desde = _data_imap(date.today() - timedelta(days=dias))
        status, dados = mail.search(None, f'(SINCE {desde} SUBJECT "Chamado #")')
        if status != "OK":
            logger.warning("IMAP search falhou")
            return resultado
        ids = dados[0].split()
        cabecalhos = fetch_em_lote(mail, ids, "(BODY.PEEK[HEADER])")
        processar = []
        for mid in ids:
            chave = mid.decode() if isinstance(mid, bytes) else str(mid)
            entrada = cabecalhos.get(chave)
            if not entrada:
                continue
            h = email.message_from_bytes(entrada[1])
            message_id = (h.get("Message-ID") or "").strip()
            if h.get(CABECALHO_PORTAL):
                continue                      # foi a gente que mandou
            if _ja_processado(conn, message_id):
                continue
            if message_id and conn.execute("SELECT 1 FROM portal_chamados_mensagens WHERE message_id = ? OR message_id_saida = ?",
                                           (message_id, message_id)).fetchone():
                _marcar_processado(conn, message_id, "")
                continue
            processar.append(mid)
        corpos = fetch_em_lote(mail, processar, "(RFC822)") if processar else {}
        for mid in processar:
            chave = mid.decode() if isinstance(mid, bytes) else str(mid)
            entrada = corpos.get(chave)
            if not entrada:
                continue
            try:
                msg = email.message_from_bytes(entrada[1])
            except Exception as e:
                logger.warning("e-mail %s ilegível: %s", chave, e)
                continue
            resultado["lidos"] += 1
            message_id = (msg.get("Message-ID") or "").strip()
            assunto = decodificar_header(msg.get("Subject", ""))
            remetente = parseaddr(decodificar_header(msg.get("From", "")))[1].lower()
            corpo = extrair_texto_corpo(msg)
            numero = _numero_chamado(assunto, corpo)
            chamado = buscar_chamado(conn, numero) if numero else None
            if not chamado:
                logger.info("e-mail de %s sem chamado conhecido (%s) -- ignorado", remetente, assunto)
                _marcar_processado(conn, message_id, remetente)
                resultado["ignorados"] += 1
                continue
            origem = _classificar_remetente(conn, chamado, remetente, config)
            if not origem:
                logger.warning("chamado %s: e-mail de remetente desconhecido %s -- ignorado", chamado["id"], remetente)
                _marcar_processado(conn, message_id, remetente)
                resultado["ignorados"] += 1
                continue
            texto = _limpar_texto_email(corpo)
            anexos = _anexos_do_email(msg, chamado["id"])
            if not texto and not anexos:
                _marcar_processado(conn, message_id, remetente)
                resultado["ignorados"] += 1
                continue
            destinatarios_ja = {a.lower() for _, a in getaddresses([decodificar_header(msg.get("To", "")),
                                                                    decodificar_header(msg.get("Cc", ""))]) if a}
            nome_remetente = parseaddr(decodificar_header(msg.get("From", "")))[0] or remetente
            try:
                if origem == ORIGEM_CLIENTE:
                    cliente = {"cnpj": chamado["cnpj_embarcador"], "nome": chamado.get("nome_cliente"), "sender_id": chamado.get("sender_id")}
                    m, chamado, avisar = registrar_mensagem_cliente(conn, chamado, cliente, texto, anexos, config,
                                                                    canal=CANAL_EMAIL, message_id=message_id, email_remetente=remetente)
                    alvo = [e for e in emails_atendimento(config) if e.lower() not in destinatarios_ja]
                    if avisar and alvo:
                        email_para_atendimento(conn, chamado, m, config)
                else:
                    m, chamado, mandar = registrar_mensagem_equipe(conn, chamado, remetente, nome_remetente.split("<")[0].strip(),
                                                                   texto, anexos, config, canal=CANAL_EMAIL,
                                                                   message_id=message_id, email_remetente=remetente)
                    alvo = [e for e in emails_do_cliente(conn, chamado["cnpj_embarcador"]) if e.lower() not in destinatarios_ja]
                    if mandar and alvo:
                        email_resposta_cliente(conn, chamado, m, alvo, config)
                resultado["gravados"] += 1
                logger.info("chamado %s: mensagem por e-mail de %s (%s) gravada", chamado["id"], remetente, origem)
            except ErroChamado as e:
                logger.warning("chamado %s: e-mail de %s não gravado: %s", chamado["id"], remetente, e)
            _marcar_processado(conn, message_id, remetente)
        try:
            mail.logout()
        except Exception:
            pass
    except Exception as e:
        logger.exception("leitor de chamados falhou: %s", e)
    finally:
        conn.close()
    return resultado


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Chamados do portal do cliente")
    sub = p.add_subparsers(dest="cmd", required=True)
    ler = sub.add_parser("ler", help="lê a caixa de e-mail dos chamados")
    ler.add_argument("--loop", action="store_true")
    ler.add_argument("--intervalo", type=int, default=None, help="minutos entre leituras (padrão: config ou 3)")
    li = sub.add_parser("listar")
    li.add_argument("--todos", action="store_true")
    r = sub.add_parser("responder")
    r.add_argument("id", type=int)
    r.add_argument("texto")
    r.add_argument("--nome", default="Fresh Log")
    rs = sub.add_parser("resolver")
    rs.add_argument("id", type=int)
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(_RAIZ / "dados" / "portal_chamados.log", encoding="utf-8")])
    config = carregar_config()

    if args.cmd == "ler":
        intervalo = args.intervalo or int(cfg_chamados(config).get("intervalo_leitura_min") or 3)
        while True:
            res = processar_emails(config)
            logger.info("leitura: %s", res)
            if not args.loop:
                return 0
            time.sleep(intervalo * 60)
            config = carregar_config()

    conn = conectar()
    try:
        if args.cmd == "listar":
            for c in listar_fila(conn, "todos" if args.todos else "abertos"):
                print(f"#{c['id']:<5} {c['status_rotulo']:<22} {c['quando']:<12} {c.get('nome_cliente') or c['cnpj_embarcador']:<28} "
                      f"{c.get('area_rotulo') or '':<22} {c.get('assunto') or ''}")
        elif args.cmd == "responder":
            ch = buscar_chamado(conn, args.id)
            if not ch:
                print("chamado não existe")
                return 2
            m, ch, mandar = registrar_mensagem_equipe(conn, ch, "cli", args.nome, args.texto, [], config)
            if mandar:
                ok = email_resposta_cliente(conn, ch, m, emails_do_cliente(conn, ch["cnpj_embarcador"]), config)
                print("e-mail pro cliente:", "ok" if ok else "FALHOU")
            print("gravado")
        elif args.cmd == "resolver":
            ch = buscar_chamado(conn, args.id)
            if not ch:
                print("chamado não existe")
                return 2
            ch = resolver(conn, ch, "cli")
            ok = email_historico(conn, ch, emails_do_cliente(conn, ch["cnpj_embarcador"]), config)
            print("resolvido; histórico:", "ok" if ok else "FALHOU")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
