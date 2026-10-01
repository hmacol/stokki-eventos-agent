# -*- coding: utf-8 -*-
"""
avisar_fora_area.py

Aviso em massa aos EMBARCADORES sobre pedidos fora da area de atendimento,
disparado pelo botao "Avisar clientes" do bloco "Fora da area" do
planejamento (Hugo, 30/09/2026). E-mail (mesmo texto da roteirizacao,
notificar_area_nao_atendida.assunto_e_conteudo) + WhatsApp no grupo que o
cliente ja tem com a Fresh (interno.whatsapp_grupo_id), pelo numero do Hugo
(notificar_whatsapp.despachar, fora do teto diario).

Nao mexe em pedidos_area_notificada (marca da roteirizacao, que continua
tirando o pedido da rota automatica e mandando o e-mail so pro Hugo). O
historico do botao fica em avisos_fora_area, uma linha por pedido e canal.

config.yaml (opcional):
    avisos_fora_area:
      forcar_destino: ""   # preenchido = e-mail vai so pra esse endereco
"""
import logging
import re
import sqlite3
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
if str(_RAIZ / "roteirizacao") not in sys.path:
    sys.path.insert(0, str(_RAIZ / "roteirizacao"))

import notificar_whatsapp  # noqa: E402
from email_utils import COR_DESTAQUE, envelope_html, enviar_email  # noqa: E402
from notificar_area_nao_atendida import TIPO_FORA_SP, TIPO_SP_NAO_ATENDIDO, assunto_e_conteudo  # noqa: E402
from regioes_dia_fixo import extrair_cidade, extrair_uf  # noqa: E402

logger = logging.getLogger(__name__)

DB_PATH = _RAIZ / "dados" / "dados.db"
TIPO_SP = TIPO_SP_NAO_ATENDIDO
MAX_PEDIDOS_WHATSAPP = 10
ORIGEM_WHATSAPP = "avisar_fora_area"
TIPO_WHATSAPP = "fora_area_cliente"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS avisos_fora_area (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id  INTEGER NOT NULL,
    codigo      TEXT,
    sender_id   INTEGER,
    tipo        TEXT NOT NULL,
    canal       TEXT NOT NULL,
    situacao    TEXT NOT NULL,
    destino     TEXT,
    por         TEXT,
    criado_em   TEXT NOT NULL
)"""

FRASES = {
    TIPO_FORA_SP: ("Esses destinos ficam fora do estado de SP. Haverá redespacho por transportadora? "
                   "Se sim, nos envie o endereço completo com CEP e o nome da transportadora."),
    TIPO_SP: "Esses destinos ficam fora da nossa área de atendimento padrão. Se quiser, fazemos uma cotação de entrega dedicada.",
}


# --- Banco ----------------------------------------------------------------------

def conectar(db_path=DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def garantir_coluna_grupo(conn) -> None:
    """ALTER TABLE idempotente: interno.whatsapp_grupo_id (30/09)."""
    colunas = [r[1] for r in conn.execute("PRAGMA table_info(interno)")]
    if colunas and "whatsapp_grupo_id" not in colunas:
        conn.execute("ALTER TABLE interno ADD COLUMN whatsapp_grupo_id TEXT")
        conn.commit()


def _emails(bruto) -> list[str]:
    return [e.strip() for e in re.split(r"[,;\t]+", bruto or "") if e.strip() and "@" in e]


def embarcadores(conn) -> dict[int, dict]:
    """sender_id -> {nome, emails, whatsapp_grupo_id} (mesmo criterio de nome
    de notificar_area_nao_atendida._carregar_embarcadores_por_sender_id)."""
    garantir_coluna_grupo(conn)
    rows = conn.execute(
        "SELECT sender_id, nome_remetente, apelido, email, whatsapp_grupo_id FROM interno WHERE sender_id IS NOT NULL"
    ).fetchall()
    return {r["sender_id"]: {"nome": r["apelido"] or r["nome_remetente"] or "",
                             "emails": _emails(r["email"]),
                             "whatsapp_grupo_id": (r["whatsapp_grupo_id"] or "").strip() or None}
            for r in rows}


def _rotulo_quando(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%d/%m %H:%M")


def avisados_por_service_id(conn) -> dict[int, dict]:
    """Ultimo envio com sucesso por pedido (qualquer canal): {service_id: {em, por}}."""
    rows = conn.execute(
        "SELECT service_id, por, MAX(criado_em) AS em FROM avisos_fora_area "
        "WHERE situacao = 'enviado' GROUP BY service_id").fetchall()
    return {r["service_id"]: {"em": _rotulo_quando(r["em"]), "por": r["por"] or "", "iso": r["em"]} for r in rows}


def _registrar(conn, agora, pedidos, sender_id, tipo, canal, situacao, destino, por):
    conn.executemany(
        "INSERT INTO avisos_fora_area (service_id, codigo, sender_id, tipo, canal, situacao, destino, por, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(p["service_id"], p["codigo"], sender_id, tipo, canal, situacao, destino, por,
          agora.isoformat(timespec="seconds")) for p in pedidos])
    conn.commit()


# --- Texto ---------------------------------------------------------------------

def _limpo(texto) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_~`]", "", str(texto or ""))).strip()


def texto_whatsapp(tipo: str, pedidos: list[dict], com_email: bool) -> str:
    """Cada item: {codigo, cidade, uf}. Sem limite de 200 caracteres (lista
    pedidos, como o aviso de insucesso); ate MAX_PEDIDOS_WHATSAPP linhas."""
    linhas = ["⚠️ *Fresh Log · pedidos fora da área de atendimento*"]
    for p in pedidos[:MAX_PEDIDOS_WHATSAPP]:
        codigo = _limpo(p.get("codigo")).lstrip("#")
        cidade, uf = _limpo(p.get("cidade")), _limpo(p.get("uf"))
        lugar = "/".join(x for x in (cidade, uf) if x)
        linhas.append(f"{codigo} · {lugar}" if lugar else codigo)
    resto = len(pedidos) - MAX_PEDIDOS_WHATSAPP
    if resto > 0:
        linhas.append(f"e mais {resto}")
    frase = FRASES.get(tipo, FRASES[TIPO_SP])
    linhas.append(frase + " Detalhes no e-mail." if com_email else frase)
    return "\n".join(linhas)


# --- Previa -----------------------------------------------------------------------

def whatsapp_disponivel(config: dict) -> bool:
    cfg = (config or {}).get("whatsapp_notificacoes") or {}
    return bool(cfg.get("ativo")) and notificar_whatsapp.integracao_openwa.configurado(cfg)


def _pedido(servico: dict) -> dict:
    return {"service_id": servico["id"], "codigo": str(servico.get("code") or "").lstrip("#").strip(),
            "cidade": extrair_cidade(servico), "uf": extrair_uf(servico)}


def _agrupar(servicos: list[dict], tipos_area: dict) -> tuple["OrderedDict[tuple, list]", list[int]]:
    """(sender_id, tipo) -> servicos, na ordem de chegada; ignorados = fora
    dos tipos_area (nao esta mais fora da area). Repetidos entram uma vez."""
    grupos: "OrderedDict[tuple, list]" = OrderedDict()
    ignorados, vistos = [], set()
    for s in servicos:
        sid = s["id"]
        if sid in vistos:
            continue
        vistos.add(sid)
        tipo = tipos_area.get(sid)
        if not tipo:
            ignorados.append(sid)
            continue
        grupos.setdefault((s.get("sender_id"), tipo), []).append(s)
    return grupos, ignorados


def montar_previa(servicos: list[dict], tipos_area: dict, config: dict, conn,
                  grupos_nomes: dict | None = None) -> dict:
    embs = embarcadores(conn)
    avisados = avisados_por_service_id(conn)
    grupos, ignorados = _agrupar(servicos, tipos_area)
    wa_ok = whatsapp_disponivel(config)
    blocos = []
    for (sender_id, tipo), lista in grupos.items():
        emb = embs.get(sender_id) or {"nome": "", "emails": [], "whatsapp_grupo_id": None}
        pedidos = []
        for s in lista:
            p = _pedido(s)
            aviso = avisados.get(s["id"])
            p["avisado_em"] = aviso["em"] if aviso else None
            p["avisado_por"] = aviso["por"] if aviso else None
            pedidos.append(p)
        avisos = [avisados[s["id"]] for s in lista if s["id"] in avisados]
        ultimo = max(avisos, key=lambda a: a["iso"]) if avisos else None
        grupo_id = emb["whatsapp_grupo_id"]
        blocos.append({
            "sender_id": sender_id, "nome": emb["nome"] or f"Remetente {sender_id}", "tipo": tipo,
            "pedidos": pedidos, "emails": emb["emails"], "whatsapp_grupo_id": grupo_id,
            "whatsapp_grupo_nome": (grupos_nomes or {}).get(grupo_id) if grupo_id else None,
            "ultimo_aviso": {"em": ultimo["em"], "por": ultimo["por"]} if ultimo else None,
            "enviavel": bool(emb["emails"] or (grupo_id and wa_ok)),
        })
    # Ordem da previa: por nome do embarcador, e dentro dele fora_sp antes
    # de sp_nao_atendido (ordem alfabetica dos tipos).
    blocos.sort(key=lambda b: (b["nome"].lower(), b["tipo"]))
    return {"blocos": blocos, "ignorados": ignorados, "whatsapp_disponivel": wa_ok}


# --- Envio ----------------------------------------------------------------------

def _forcar_destino(config: dict) -> str:
    return str(((config or {}).get("avisos_fora_area") or {}).get("forcar_destino") or "").strip()


def enviar(itens: list[dict], servicos: list[dict], tipos_area: dict, config: dict, por: str, conn,
           modo_teste: bool = False, agora: datetime | None = None) -> dict:
    """itens: [{sender_id, tipo, service_ids, canais}]. Reclassifica com
    tipos_area (pula o que saiu da area entre a previa e o clique), manda
    canal a canal e registra. Falha num bloco nao para o proximo."""
    agora = agora or datetime.now()
    por_id = {s["id"]: s for s in servicos}
    embs = embarcadores(conn)
    forcar = _forcar_destino(config)
    resultados, ignorados = [], []
    for item in itens:
        sender_id, tipo = item.get("sender_id"), item.get("tipo")
        canais = set(item.get("canais") or [])
        lista = []
        for sid in dict.fromkeys(item.get("service_ids") or []):
            s = por_id.get(sid)
            if not s or tipos_area.get(sid) != tipo:
                ignorados.append(sid)
                continue
            lista.append(s)
        r = {"sender_id": sender_id, "tipo": tipo, "email": "pulado", "whatsapp": "pulado", "detalhe": ""}
        resultados.append(r)
        if not lista:
            r["detalhe"] = "nenhum pedido fora da área"
            continue
        emb = embs.get(sender_id) or {"nome": "", "emails": [], "whatsapp_grupo_id": None}
        pedidos = [_pedido(s) for s in lista]
        faltas = []

        quer_email = "email" in canais
        if quer_email and not emb["emails"] and not forcar:
            faltas.append("sem e-mail")
            quer_email = False
        if quer_email:
            assunto, conteudo = assunto_e_conteudo(emb["nome"], tipo, lista)
            corpo = envelope_html(conteudo, rodape="Mensagem enviada pela equipe Fresh Log.", cor_acento=COR_DESTAQUE)
            destinos = [forcar] if forcar else emb["emails"]
            if modo_teste:
                logger.info(f"[MODO TESTE] e-mail nao enviado para {destinos}: {assunto}")
                r["email"] = "modo_teste"
            else:
                ok = enviar_email(destinos, assunto, corpo, (config or {}).get("email", {}))
                r["email"] = "enviado" if ok else "falhou"
                _registrar(conn, agora, pedidos, sender_id, tipo, "email", r["email"], "; ".join(destinos), por)

        quer_wa = "whatsapp" in canais
        if quer_wa and not emb["whatsapp_grupo_id"]:
            faltas.append("sem grupo de WhatsApp")
            quer_wa = False
        if quer_wa:
            texto = texto_whatsapp(tipo, pedidos, com_email=r["email"] in ("enviado", "modo_teste"))
            situacao = notificar_whatsapp.despachar(config, ORIGEM_WHATSAPP, TIPO_WHATSAPP, texto, None,
                                                    modo_teste=modo_teste, conn=conn, agora=agora,
                                                    grupo_id=emb["whatsapp_grupo_id"], contar_no_teto=False)
            r["whatsapp"] = situacao
            if situacao in ("enviado", "falhou", "nao_enviado"):
                _registrar(conn, agora, pedidos, sender_id, tipo, "whatsapp",
                           "enviado" if situacao == "enviado" else "falhou", emb["whatsapp_grupo_id"], por)
        r["detalhe"] = ", ".join(faltas)
    return {"resultados": resultados, "ignorados": ignorados}
