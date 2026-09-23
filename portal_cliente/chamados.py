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

# Quem abriu o chamado (Hugo, 12/09/2026: o motorista ganhou chat no app e
# cai na MESMA fila da tela /atendimento, numa aba própria). O cliente é o
# embarcador (portal); o motorista é quem está na rua (app de motoristas).
TIPO_CLIENTE = "CLIENTE"
TIPO_MOTORISTA = "MOTORISTA"

# Perfil de atendimento: cada um tem horário e caixa de e-mail próprios
# (o motorista roda cedo, o cliente fala em horário comercial).
PERFIL_CLIENTE = "cliente"
PERFIL_LOGISTICA = "logistica"

AREAS = {
    "entrega": "Entrega",
    "coleta": "Coleta / retirada",
    "avaria_falta": "Avaria ou falta",
    "comprovante": "Comprovante / canhoto",
    "financeiro": "Financeiro",
    "cadastro": "Cadastro / acesso",
    "outro": "Outro",
    "envios": "Envios do portal",  # bloqueio de área não atendida (23/09), aberto pelo sistema
}

# Assuntos do motorista (Hugo, 12/09) -- o que ele escolhe ao abrir a conversa.
AREAS_MOTORISTA = {
    "entrega_problema": "Problema na entrega",
    "veiculo": "Veículo, acidente ou atraso",
    "pagamento": "Pagamento (rota, km, pedágio)",
    "app": "Problema no app",
    "outro": "Outro",
}


def areas_do_tipo(tipo: str | None) -> dict:
    return AREAS_MOTORISTA if tipo == TIPO_MOTORISTA else AREAS


def perfil_do_tipo(tipo: str | None) -> str:
    return PERFIL_LOGISTICA if tipo == TIPO_MOTORISTA else PERFIL_CLIENTE

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
    (09/09): remetente entregas@freshlogbr.com, que é ALIAS de hugo@ --
    então o login (SMTP e IMAP) continua sendo hugo@ com a senha de app
    dela (`usuario_login` + `senha_app` da seção email:), e o que chega em
    entregas@ já cai na mesma caixa lida pelo leitor."""
    global _avisou_senha_vazia
    base = dict(config.get("email", {}) or {})
    propria = cfg_chamados(config).get("email", {}) or {}
    base.update({k: v for k, v in propria.items() if v})
    if not base.get("usuario_login"):
        # sem usuario_login explícito, autentica com a conta da seção email:
        base["usuario_login"] = (config.get("email", {}) or {}).get("remetente") or base.get("remetente")
    if propria.get("remetente") and not (propria.get("senha_app") or base.get("senha_app")) and not _avisou_senha_vazia:
        _avisou_senha_vazia = True
        logger.warning("chamados: sem senha_app nem na seção email: -- e-mails dos chamados não vão sair")
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


def emails_atendimento(config: dict, perfil: str = PERFIL_CLIENTE) -> list[str]:
    """Caixa da equipe. A logística pode ter a própria
    (chamados.email_logistica); sem ela, cai na do atendimento."""
    c = cfg_chamados(config)
    lista = None
    if perfil == PERFIL_LOGISTICA:
        lista = c.get("email_logistica")
    lista = lista or c.get("email_atendimento") or (config.get("email", {}) or {}).get("email_atendimento") \
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
    _migrar(conn)
    conn.commit()
    return conn


# Colunas novas de portal_chamados (12/09: chamado aberto pelo MOTORISTA no
# app). Migração aditiva -- banco criado em 09/09 não tem essas colunas.
# `cnpj_embarcador` (NOT NULL desde o desenho do portal) fica '' no chamado
# de motorista: nenhuma consulta de cliente casa com string vazia.
_COLUNAS_NOVAS = [
    ("tipo", f"TEXT NOT NULL DEFAULT '{TIPO_CLIENTE}'"),
    ("motorista_cpf", "TEXT"),
    ("agent_id", "INTEGER"),
    ("rota_id", "INTEGER"),          # nucleo_rotas.id em foco, quando houver
    ("parada_id", "INTEGER"),        # nucleo_paradas.id em foco, quando houver
]


def _migrar(conn: sqlite3.Connection) -> None:
    existentes = {r[1] for r in conn.execute("PRAGMA table_info(portal_chamados)")}
    for nome, tipo in _COLUNAS_NOVAS:
        if nome not in existentes:
            conn.execute(f"ALTER TABLE portal_chamados ADD COLUMN {nome} {tipo}")
            logger.info("chamados: coluna portal_chamados.%s adicionada", nome)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_portal_chamados_tipo ON portal_chamados (tipo, status, atualizado_em)")


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


# Horário da logística (Hugo, 12/09): o motorista sai antes das 7h e roda
# sábado; o atendimento dele é mais largo que o do cliente e sem almoço
# (a equipe reveza). Editável em portal_cliente.chamados.horario_logistica.
_HORARIO_PADRAO = {
    PERFIL_CLIENTE: {"dias": [0, 1, 2, 3, 4], "inicio": "08:30", "fim": "17:00",
                     "almoco_inicio": "13:00", "almoco_fim": "14:00"},
    PERFIL_LOGISTICA: {"dias": [0, 1, 2, 3, 4, 5], "inicio": "06:00", "fim": "19:00",
                       "almoco_inicio": "", "almoco_fim": ""},
}


def horario_config(config: dict, perfil: str = PERFIL_CLIENTE) -> dict:
    padrao = _HORARIO_PADRAO.get(perfil, _HORARIO_PADRAO[PERFIL_CLIENTE])
    chave = "horario_logistica" if perfil == PERFIL_LOGISTICA else "horario"
    h = cfg_chamados(config).get(chave, {}) or {}
    # Sem almoço configurado (logística): janela vazia -- _hm devolve o
    # padrão, então um almoço de 00:00 a 00:00 nunca pega.
    almoco_ini = h.get("almoco_inicio", padrao["almoco_inicio"]) or "00:00"
    almoco_fim = h.get("almoco_fim", padrao["almoco_fim"]) or "00:00"
    return {
        "dias": [int(d) for d in (h.get("dias") or padrao["dias"])],   # 0 = segunda
        "inicio": _hm(h.get("inicio"), padrao["inicio"]),
        "fim": _hm(h.get("fim"), padrao["fim"]),
        "almoco_inicio": _hm(almoco_ini, "00:00"),
        "almoco_fim": _hm(almoco_fim, "00:00"),
    }


def _fmt_hm(hm: tuple[int, int]) -> str:
    h, m = hm
    return f"{h}h{m:02d}" if m else f"{h}h"


def texto_horario(config: dict, perfil: str = PERFIL_CLIENTE) -> str:
    hc = horario_config(config, perfil)
    dias = hc["dias"]
    if dias == [0, 1, 2, 3, 4]:
        d = "seg a sex"
    elif dias == [0, 1, 2, 3, 4, 5]:
        d = "seg a sáb"
    else:
        d = ", ".join(DIAS_SEMANA[i] for i in dias)
    texto = f"{d}, {_fmt_hm(hc['inicio'])} às {_fmt_hm(hc['fim'])}"
    if hc["almoco_inicio"] != hc["almoco_fim"]:
        texto += f" · almoço {_fmt_hm(hc['almoco_inicio'])}–{_fmt_hm(hc['almoco_fim'])}"
    return texto


def situacao_horario(config: dict, agora: datetime | None = None, perfil: str = PERFIL_CLIENTE) -> dict:
    """{dentro: bool, motivo: None|'almoco'|'antes'|'depois'|'fora_dia', volta_em: str}."""
    agora = agora or datetime.now()
    hc = horario_config(config, perfil)
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


def situacao_atendimento(conn: sqlite3.Connection, config: dict, agora: datetime | None = None,
                         perfil: str = PERFIL_CLIENTE) -> dict:
    """O que o chat (widget do cliente / aba Ajuda do app) mostra no
    cabeçalho. `perfil` escolhe o horário: cliente ou logística."""
    h = situacao_horario(config, agora, perfil)
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
            "horario": texto_horario(config, perfil), "atendentes_online": len(online), "perfil": perfil,
            "nomes_online": [a["nome"] or a["usuario"] for a in online]}


# ── Chamados ───────────────────────────────────────────────────────────────────

def _chamado_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d.setdefault("tipo", TIPO_CLIENTE)
    d["tipo"] = d.get("tipo") or TIPO_CLIENTE
    d["de_motorista"] = d["tipo"] == TIPO_MOTORISTA
    d["status_rotulo"] = ROTULOS_STATUS.get(d["status"], d["status"])
    d["area_rotulo"] = areas_do_tipo(d["tipo"]).get(d.get("area") or "", d.get("area") or "")
    # Nome de quem abriu -- é o que a fila e os e-mails mostram, seja
    # embarcador ou motorista.
    d["solicitante"] = d.get("nome_cliente") or (d.get("motorista_cpf") if d["de_motorista"] else d.get("cnpj_embarcador")) or ""
    d["quando"] = rotulo_quando(d.get("ultima_msg_em") or d.get("criado_em"))
    d["espera"] = _espera(d.get("fila_desde") or d.get("ultima_msg_em"))
    d["aberto"] = d["status"] in STATUS_ABERTOS
    try:
        d["pedido_dados"] = json.loads(d["pedido_dados"]) if d.get("pedido_dados") else None
    except (TypeError, ValueError):
        d["pedido_dados"] = None
    return d


def criar_chamado(conn: sqlite3.Connection, solicitante: dict, origem: str, status: str, assunto: str = "",
                  area: str = "", pedido_ref: str = "", etapa_assistente: str | None = None) -> dict:
    """`solicitante`: cliente do portal ({cnpj, sender_id, nome}) ou motorista
    do app ({tipo: MOTORISTA, cpf, agent_id, nome, rota_id})."""
    agora = _agora()
    tipo = solicitante.get("tipo") or TIPO_CLIENTE
    cur = conn.execute("""
        INSERT INTO portal_chamados (cnpj_embarcador, sender_id, nome_cliente, assunto, area, pedido_ref, status, origem,
                                     etapa_assistente, fila_desde, criado_em, atualizado_em,
                                     tipo, motorista_cpf, agent_id, rota_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (solicitante.get("cnpj") or "", solicitante.get("sender_id"), solicitante.get("nome"), assunto[:120], area,
          pedido_ref[:40], status, origem, etapa_assistente,
          agora if status in (STATUS_NA_FILA, STATUS_AGUARDANDO_FL) else None, agora, agora,
          tipo, solicitante.get("cpf"), solicitante.get("agent_id"), solicitante.get("rota_id")))
    conn.commit()
    return buscar_chamado(conn, cur.lastrowid)


def buscar_chamado_motorista(conn: sqlite3.Connection, chamado_id: int, cpf: str) -> dict | None:
    r = conn.execute("SELECT * FROM portal_chamados WHERE id = ? AND tipo = ? AND motorista_cpf = ?",
                     (chamado_id, TIPO_MOTORISTA, cpf)).fetchone()
    return _chamado_dict(r) if r else None


def listar_chamados_motorista(conn: sqlite3.Connection, cpf: str) -> list[dict]:
    limite = (datetime.now() - timedelta(days=DIAS_LISTAGEM_CLIENTE)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute("""
        SELECT c.*, (SELECT COUNT(*) FROM portal_chamados_mensagens m
                     WHERE m.chamado_id = c.id AND m.origem IN ('equipe','assistente','sistema') AND m.lido_cliente = 0) AS nao_lidas,
               (SELECT texto FROM portal_chamados_mensagens m WHERE m.chamado_id = c.id ORDER BY m.id DESC LIMIT 1) AS ultima_texto,
               (SELECT origem FROM portal_chamados_mensagens m WHERE m.chamado_id = c.id ORDER BY m.id DESC LIMIT 1) AS ultima_origem_msg
        FROM portal_chamados c
        WHERE c.tipo = ? AND c.motorista_cpf = ? AND c.criado_em >= ?
        ORDER BY (c.status = 'RESOLVIDO'), c.atualizado_em DESC
    """, (TIPO_MOTORISTA, cpf, limite)).fetchall()
    return [_chamado_dict(r) for r in rows]


def chamado_ativo_motorista(conn: sqlite3.Connection, cpf: str) -> dict | None:
    r = conn.execute("""
        SELECT * FROM portal_chamados WHERE tipo = ? AND motorista_cpf = ? AND status != 'RESOLVIDO'
        ORDER BY atualizado_em DESC LIMIT 1
    """, (TIPO_MOTORISTA, cpf)).fetchone()
    return _chamado_dict(r) if r else None


def nao_lidas_motorista(conn: sqlite3.Connection, cpf: str) -> int:
    """Badge da aba Ajuda: só conta o que é RESPOSTA (equipe ou assistente).
    Aviso de sistema ("entrou na conversa", "você está na fila") não acende
    badge -- o motorista está dirigindo, o badge tem que significar
    "responderam você"."""
    return conn.execute("""
        SELECT COUNT(*) FROM portal_chamados_mensagens m JOIN portal_chamados c ON c.id = m.chamado_id
        WHERE c.tipo = ? AND c.motorista_cpf = ? AND m.origem IN ('equipe','assistente') AND m.lido_cliente = 0
    """, (TIPO_MOTORISTA, cpf)).fetchone()[0]


def buscar_chamado(conn: sqlite3.Connection, chamado_id: int, cnpj: str | None = None) -> dict | None:
    """Com `cnpj`, só devolve chamado DE CLIENTE daquele embarcador -- o
    chamado de motorista guarda cnpj_embarcador vazio e nunca pode aparecer
    pra um cliente."""
    sql = "SELECT * FROM portal_chamados WHERE id = ?"
    args: list = [chamado_id]
    if cnpj:
        sql += " AND cnpj_embarcador = ? AND tipo = ?"
        args += [cnpj, TIPO_CLIENTE]
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
        WHERE c.cnpj_embarcador = ? AND c.tipo = ? AND c.criado_em >= ?
        ORDER BY (c.status = 'RESOLVIDO'), c.atualizado_em DESC
    """, (cnpj, TIPO_CLIENTE, limite)).fetchall()
    return [_chamado_dict(r) for r in rows]


def chamado_ativo_cliente(conn: sqlite3.Connection, cnpj: str) -> dict | None:
    """A conversa que o widget abre por padrão: o chamado aberto mais
    recente do cliente (chat ao vivo ou chamado aguardando)."""
    r = conn.execute("""
        SELECT * FROM portal_chamados WHERE cnpj_embarcador = ? AND tipo = ? AND status != 'RESOLVIDO'
        ORDER BY atualizado_em DESC LIMIT 1
    """, (cnpj, TIPO_CLIENTE)).fetchone()
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
        "motoristas": f"c.tipo = '{TIPO_MOTORISTA}' AND c.status != 'RESOLVIDO'",
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
        "motoristas": q("SELECT COUNT(*) FROM portal_chamados WHERE tipo = ? AND status != 'RESOLVIDO'", TIPO_MOTORISTA),
        "motoristas_na_fila": q("SELECT COUNT(*) FROM portal_chamados WHERE tipo = ? AND status IN ('NA_FILA','AGUARDANDO_FL')", TIPO_MOTORISTA),
        "resolvidos": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'RESOLVIDO'"),
        "resolvidos_hoje": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'RESOLVIDO' AND resolvido_em >= ?", hoje),
        "assistente_resolveu_hoje": q("SELECT COUNT(*) FROM portal_chamados WHERE status = 'RESOLVIDO' AND resolvido_por = 'assistente' AND resolvido_em >= ?", hoje),
        "nao_lidas_cliente": q("SELECT COUNT(*) FROM portal_chamados_mensagens m JOIN portal_chamados c ON c.id = m.chamado_id "
                               "WHERE m.origem = 'cliente' AND m.lido_equipe = 0 AND c.status != 'RESOLVIDO'"),
    }


def nao_lidas_cliente(conn: sqlite3.Connection, cnpj: str) -> int:
    return conn.execute("""
        SELECT COUNT(*) FROM portal_chamados_mensagens m JOIN portal_chamados c ON c.id = m.chamado_id
        WHERE c.cnpj_embarcador = ? AND c.tipo = ? AND m.origem IN ('equipe','assistente','sistema') AND m.lido_cliente = 0
    """, (cnpj, TIPO_CLIENTE)).fetchone()[0]


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
    """Solicitante pediu atendente (ou o assistente encaminhou). Online ->
    NA_FILA; fora do horário / sem atendente -> AGUARDANDO_FL (vira chamado
    por e-mail). O horário considerado é o do perfil do chamado."""
    sit = situacao_atendimento(conn, config, perfil=perfil_do_tipo(chamado.get("tipo")))
    if sit["estado"] == "online":
        posicao = conn.execute("SELECT COUNT(*) FROM portal_chamados WHERE status = 'NA_FILA'").fetchone()[0] + 1
        atualizar_chamado(conn, chamado["id"], status=STATUS_NA_FILA, fila_desde=_agora())
        nomes = ", ".join(sit["nomes_online"][:2])
        mensagem_sistema(conn, chamado, f"Você está na fila ({posicao}º). {nomes or 'A equipe'} vai assumir em instantes.")
        return {"status": STATUS_NA_FILA, "posicao": posicao, "online": True}
    atualizar_chamado(conn, chamado["id"], status=STATUS_AGUARDANDO_FL, fila_desde=_agora())
    onde = "aqui" if chamado.get("tipo") == TIPO_MOTORISTA else "aqui e no seu e-mail"
    if sit["estado"] == "ausente":
        txt = f"Nossos atendentes estão ocupados agora. Deixamos seu chamado registrado: a equipe responde {onde} assim que possível."
    else:
        txt = (f"{sit['texto']}. Deixamos seu chamado registrado: a equipe responde {onde} "
               f"quando voltar ({sit['horario']}).")
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
    sit = situacao_atendimento(conn, config, perfil=perfil_do_tipo(chamado.get("tipo")))
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
    onde = "App do motorista" if chamado.get("tipo") == TIPO_MOTORISTA else "Portal do cliente"
    return (f"Fresh Log · {onde} · chamado #{chamado['id']} · {url_base(config)}<br>"
            f"<span style='color:#9CA3AF'>Mantenha o número do chamado no assunto ao responder. "
            f"<span style='font-family:Consolas,monospace'>[[CHAMADO:{chamado['id']}]]</span></span>")


def _botao_ver(chamado: dict, config: dict, rotulo: str = "Ver o chamado no portal") -> str:
    """Botão 'abrir no portal'. O motorista não tem portal: ele acompanha a
    conversa na aba Ajuda do app, então o e-mail dele vai sem botão."""
    if chamado.get("tipo") == TIPO_MOTORISTA:
        return "<p style='margin:8px 0 2px;font-size:13px;color:#6B7280'>A conversa também está na aba <b>Ajuda</b> do app do motorista.</p>"
    return (f"<p style='margin:8px 0 2px'><a href='{url_base(config)}/?chamado={chamado['id']}' "
            f"style='display:inline-block;background:#0EA575;color:#fff;padding:12px 22px;border-radius:7px;"
            f"text-decoration:none;font-weight:700'>{rotulo}</a></p>")


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
    """Aviso pra equipe: chamado novo deixado fora do horário, ou mensagem do
    solicitante num chamado que ninguém está vendo. Chamado de motorista vai
    pra caixa da logística (chamados.email_logistica)."""
    perfil = perfil_do_tipo(chamado.get("tipo"))
    de_motorista = chamado.get("tipo") == TIPO_MOTORISTA
    titulo = ("deixou o chamado" if novo else "mandou uma mensagem no chamado")
    ctx = "" if not chamado.get("resumo_assistente") else \
        f"<tr><td style='padding:6px 0;color:#6B7280;vertical-align:top'>Triagem do assistente</td><td style='padding:6px 0'>{_html_texto(chamado['resumo_assistente'])}</td></tr>"
    quem = "motorista" if de_motorista else "cliente"
    onde = "app" if de_motorista else "portal"
    linha_rota = ""
    if de_motorista and chamado.get("rota_id"):
        linha_rota = (f"<tr><td style='padding:6px 0;color:#6B7280'>Rota</td>"
                      f"<td style='padding:6px 0'>#{chamado['rota_id']}</td></tr>")
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'>O {quem} <b>{html.escape(chamado.get('solicitante') or '')}</b> {titulo} "
        f"<b>#{chamado['id']}</b> pelo {'e-mail' if msg.get('canal') == CANAL_EMAIL else onde}"
        f"{' <b>fora do horário de atendimento</b>' if novo and chamado.get('origem') == 'chamado' else ''}.</p>"
        f"<table style='border-collapse:collapse;font-size:13px;width:100%'>"
        f"<tr><td style='padding:6px 0;color:#6B7280;width:150px'>Assunto</td><td style='padding:6px 0;font-weight:600'>{html.escape(chamado.get('assunto') or '(sem assunto)')}</td></tr>"
        f"<tr><td style='padding:6px 0;color:#6B7280'>Área</td><td style='padding:6px 0'>{html.escape(chamado.get('area_rotulo') or '-')}</td></tr>"
        f"<tr><td style='padding:6px 0;color:#6B7280'>{'Parada / pedido' if de_motorista else 'Pedido / NF'}</td><td style='padding:6px 0;font-family:Consolas,monospace'>{html.escape(chamado.get('pedido_ref') or '-')}</td></tr>"
        f"{linha_rota}"
        f"<tr><td style='padding:6px 0;color:#6B7280'>Situação</td><td style='padding:6px 0'>{chamado['status_rotulo']}</td></tr>{ctx}</table>"
        f"<div style='margin:14px 0'>{_bloco_mensagem(msg, '#F5A623')}</div>"
        f"<p style='margin:8px 0 2px'><a href='{url_painel(config)}/atendimento?chamado={chamado['id']}' style='display:inline-block;background:#141428;color:#fff;padding:12px 22px;border-radius:7px;text-decoration:none;font-weight:700'>Abrir na tela de Atendimento</a></p>"
        f"<div style='background:#E6FBF5;border-radius:8px;padding:12px 16px;font-size:13px;margin-top:14px'><b>Pra responder ao {quem}, é só responder este e-mail.</b> "
        + (f"O texto aparece no app do motorista em alguns minutos." if de_motorista
           else "O texto vai pro cliente e entra no chamado em alguns minutos. Anexos também vão.")
        + " Ou responda pela tela de Atendimento do painel.</div>",
        rodape=_rodape(chamado, config), cor_acento="#F5A623")
    return _mandar(conn, chamado, msg, emails_atendimento(config, perfil),
                   _assunto_email(chamado) + f" · {chamado.get('solicitante') or ''}".rstrip(" ·"),
                   corpo, config, anexos=_anexos_email(chamado["id"], msg))


def email_confirmacao_cliente(conn, chamado: dict, msg: dict, emails: list[str], config: dict) -> bool:
    """Cliente deixou um chamado fora do horário: confirma por e-mail (e
    assim ele já pode responder por e-mail também)."""
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'>Olá, <b>{html.escape(chamado.get('nome_cliente') or '')}</b>. Recebemos seu chamado "
        f"<b>#{chamado['id']} · {html.escape(chamado.get('assunto') or '')}</b>.</p>"
        f"<div style='margin:14px 0'>{_bloco_mensagem(msg, '#2A78D6')}</div>"
        f"<p style='margin:0 0 12px;font-size:13.5px'>A equipe Fresh Log responde aqui por e-mail e no portal. Nosso horário: "
        f"{html.escape(texto_horario(config, perfil_do_tipo(chamado.get('tipo'))))}. "
        f"Pra complementar, é só <b>responder este e-mail</b>.</p>"
        + _botao_ver(chamado, config),
        rodape=_rodape(chamado, config))
    return _mandar(conn, chamado, None, emails, _assunto_email(chamado), corpo, config)


def email_resposta_cliente(conn, chamado: dict, msg: dict, emails: list[str], config: dict) -> bool:
    corpo = envelope_html(
        f"<p style='margin:0 0 12px'>Olá, <b>{html.escape(chamado.get('nome_cliente') or '')}</b>. A Fresh Log respondeu ao seu chamado "
        f"<b>#{chamado['id']} · {html.escape(chamado.get('assunto') or '')}</b>:</p>"
        f"<div style='margin:14px 0'>{_bloco_mensagem(msg, '#00C896')}</div>"
        f"<p style='margin:0 0 12px;font-size:13.5px'>Pra continuar a conversa, <b>responda este e-mail</b>. Fotos e PDF anexados também entram no chamado.</p>"
        + _botao_ver(chamado, config)
        + f"<p style='margin:12px 0 0;font-size:12.5px;color:#6B7280'>Quando o assunto estiver resolvido, marque o chamado como resolvido. Uma nova mensagem reabre o chamado.</p>",
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
        f"<p style='margin:14px 0 0;font-size:13px'>Ficou algo pendente? <b>Responda este e-mail</b> e o chamado #{chamado['id']} reabre automaticamente — ou fale com a gente pelo chat.</p>"
        + _botao_ver(chamado, config),
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


def emails_do_motorista(conn: sqlite3.Connection, cpf: str) -> list[str]:
    """E-mail do motorista, quando houver (motoristas.email é opcional --
    a maioria só usa o app). Sem e-mail, a conversa vive só no app."""
    if not cpf:
        return []
    try:
        r = conn.execute("SELECT email FROM motoristas WHERE cpf = ?", (cpf,)).fetchone()
    except sqlite3.OperationalError:
        return []
    email = (r["email"] or "").strip() if r else ""
    return [email] if email and "@" in email else []


def emails_do_solicitante(conn: sqlite3.Connection, chamado: dict) -> list[str]:
    """Pra quem vai a resposta da equipe por e-mail: o embarcador ou o
    motorista do chamado."""
    if chamado.get("tipo") == TIPO_MOTORISTA:
        return emails_do_motorista(conn, chamado.get("motorista_cpf") or "")
    return emails_do_cliente(conn, chamado["cnpj_embarcador"])


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
    """'cliente' se o e-mail é de quem abriu o chamado (embarcador ou
    motorista); 'equipe' se é da Fresh Log; None = desconhecido (ignora, por
    segurança)."""
    remetente = (remetente or "").lower()
    if not remetente:
        return None
    emails_cli = [e.lower() for e in emails_do_solicitante(conn, chamado)]
    if remetente in emails_cli:
        return ORIGEM_CLIENTE
    dominio = remetente.split("@")[-1]
    internos = {e.lower() for e in emails_atendimento(config, perfil_do_tipo(chamado.get("tipo")))}
    internos |= {e.lower() for e in emails_atendimento(config)}
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
    usuario, senha = cfg.get("usuario_login") or cfg.get("remetente", ""), cfg.get("senha_app") or cfg.get("senha", "")
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
                    solicitante = {"cnpj": chamado["cnpj_embarcador"], "nome": chamado.get("solicitante"),
                                   "sender_id": chamado.get("sender_id"), "tipo": chamado.get("tipo")}
                    m, chamado, avisar = registrar_mensagem_cliente(conn, chamado, solicitante, texto, anexos, config,
                                                                    canal=CANAL_EMAIL, message_id=message_id, email_remetente=remetente)
                    alvo = [e for e in emails_atendimento(config, perfil_do_tipo(chamado.get("tipo")))
                            if e.lower() not in destinatarios_ja]
                    if avisar and alvo:
                        email_para_atendimento(conn, chamado, m, config)
                else:
                    m, chamado, mandar = registrar_mensagem_equipe(conn, chamado, remetente, nome_remetente.split("<")[0].strip(),
                                                                   texto, anexos, config, canal=CANAL_EMAIL,
                                                                   message_id=message_id, email_remetente=remetente)
                    alvo = [e for e in emails_do_solicitante(conn, chamado) if e.lower() not in destinatarios_ja]
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
                print(f"#{c['id']:<5} {'motorista' if c['de_motorista'] else 'cliente':<10} {c['status_rotulo']:<22} "
                      f"{c['quando']:<12} {(c.get('solicitante') or '')[:26]:<28} "
                      f"{c.get('area_rotulo') or '':<24} {c.get('assunto') or ''}")
        elif args.cmd == "responder":
            ch = buscar_chamado(conn, args.id)
            if not ch:
                print("chamado não existe")
                return 2
            m, ch, mandar = registrar_mensagem_equipe(conn, ch, "cli", args.nome, args.texto, [], config)
            if mandar:
                alvo = emails_do_solicitante(conn, ch)
                ok = email_resposta_cliente(conn, ch, m, alvo, config) if alvo else False
                print("e-mail pro solicitante:", "ok" if ok else ("sem e-mail" if not alvo else "FALHOU"))
            print("gravado")
        elif args.cmd == "resolver":
            ch = buscar_chamado(conn, args.id)
            if not ch:
                print("chamado não existe")
                return 2
            ch = resolver(conn, ch, "cli")
            alvo = emails_do_solicitante(conn, ch)
            ok = email_historico(conn, ch, alvo, config) if alvo else False
            print("resolvido; histórico:", "ok" if ok else ("sem e-mail" if not alvo else "FALHOU"))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
