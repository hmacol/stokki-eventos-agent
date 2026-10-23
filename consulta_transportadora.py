# -*- coding: utf-8 -*-
"""
consulta_transportadora.py

Fluxo automatico para transportadora desconhecida: cria o cadastro PENDENTE,
guarda os pedidos parados e uma consulta por (transportadora, embarcador).
Quando a transportadora (ou a equipe) responde, o cadastro vira ATIVA e todos
os pedidos parados dela sao liberados. Tabelas:
  transportadora_pedidos_parados   1 linha por pedido segurado (liberado_em ao responder)
  transportadora_consultas         1 linha por par transportadora x embarcador
"""
import html
import json
import logging
import re
import sqlite3
from datetime import date, datetime

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import email_utils
import notificar_whatsapp
import preferencias_notificacao
import transportadoras_cadastro as tc
from regras.transportadoras import _normalizar
from transportadoras_cadastro import DB_PATH, ErroCadastro

logger = logging.getLogger(__name__)

_SALT = "consulta-transportadora"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transportadora_pedidos_parados (
    pedido TEXT PRIMARY KEY,
    transportadora_id INTEGER, embarcador_cnpj TEXT, stkkc_id INTEGER,
    desde TEXT, liberado_em TEXT, resumo TEXT
);
CREATE INDEX IF NOT EXISTS idx_transp_parados_tid ON transportadora_pedidos_parados(transportadora_id);
CREATE TABLE IF NOT EXISTS transportadora_consultas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transportadora_id INTEGER, embarcador_cnpj TEXT,
    criado_em TEXT, enviado_em TEXT, ultimo_lembrete_em TEXT, alerta_equipe_em TEXT,
    respondido_em TEXT, respondido_por TEXT, canal_resposta TEXT,
    UNIQUE(transportadora_id, embarcador_cnpj)
);
"""


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conectar_consultas(db_path=DB_PATH):
    conn = tc.conectar(db_path)
    conn.executescript(_SCHEMA)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(transportadora_pedidos_parados)")]
    if "resumo" not in cols:  # banco criado antes do aviso ao embarcador
        conn.execute("ALTER TABLE transportadora_pedidos_parados ADD COLUMN resumo TEXT")
    conn.commit()
    return conn


def _achar_pendente(conn, nome: str, cnpj: str) -> dict | None:
    norm = _normalizar(nome)
    por_nome = None
    for r in conn.execute("SELECT * FROM transportadoras WHERE situacao='PENDENTE' ORDER BY id"):
        if cnpj and r["cnpj"] == cnpj:
            return dict(r)
        if por_nome is None and _normalizar(r["nome"]) == norm:
            por_nome = dict(r)
    return por_nome


def registrar_desconhecida(conn, nome: str, cnpj: str, pedido: str, embarcador_cnpj: str,
                           stkkc_id: int | None, resumo: dict | None = None) -> dict:
    cnpj = tc.normalizar_cnpj(cnpj)
    transp = _achar_pendente(conn, nome, cnpj)
    nova = transp is None
    if nova:
        transp = tc.criar(conn, {"nome": nome, "cnpj": cnpj, "situacao": "PENDENTE", "tipo": None},
                          origem="AUTOMATICO", por="automatico")
    tid = transp["id"]
    agora = _agora()
    conn.execute(
        "INSERT INTO transportadora_pedidos_parados (pedido, transportadora_id, embarcador_cnpj, stkkc_id, desde, resumo) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(pedido) DO UPDATE SET transportadora_id=excluded.transportadora_id, "
        "embarcador_cnpj=excluded.embarcador_cnpj, stkkc_id=excluded.stkkc_id, liberado_em=NULL, "
        "resumo=COALESCE(excluded.resumo, resumo)",
        (pedido, tid, embarcador_cnpj, stkkc_id, agora, json.dumps(resumo, ensure_ascii=False) if resumo else None))
    conn.execute(
        "INSERT INTO transportadora_consultas (transportadora_id, embarcador_cnpj, criado_em) VALUES (?,?,?) "
        "ON CONFLICT(transportadora_id, embarcador_cnpj) DO NOTHING", (tid, embarcador_cnpj, agora))
    conn.commit()
    consulta = conn.execute(
        "SELECT * FROM transportadora_consultas WHERE transportadora_id=? AND embarcador_cnpj=?",
        (tid, embarcador_cnpj)).fetchone()
    return {"transportadora": transp, "consulta": dict(consulta), "nova": nova}


def pedidos_parados(conn, transportadora_id: int, embarcador_cnpj: str | None = None) -> list[dict]:
    sql = "SELECT * FROM transportadora_pedidos_parados WHERE transportadora_id=? AND liberado_em IS NULL"
    params = [transportadora_id]
    if embarcador_cnpj is not None:
        sql += " AND embarcador_cnpj=?"
        params.append(embarcador_cnpj)
    out = []
    for r in conn.execute(sql + " ORDER BY desde, pedido", params):
        d = dict(r)
        try:
            d["resumo"] = json.loads(d["resumo"]) if d.get("resumo") else {}
        except ValueError:
            d["resumo"] = {}
        out.append(d)
    return out


def responder(conn, transportadora_id: int, tipo: str, endereco: dict | None, email: str,
              por: str, canal: str) -> dict:
    atual = tc.buscar(conn, transportadora_id)
    if atual is None:
        raise ErroCadastro(f"Transportadora {transportadora_id} nao existe.")
    if atual["situacao"] == "ATIVA":
        raise ErroCadastro("ja respondida")
    tipo = (tipo or "").strip().upper()
    if tipo not in tc.TIPOS:
        raise ErroCadastro(f"Tipo invalido: {tipo!r}.")
    mud = {"tipo": tipo, "situacao": "ATIVA"}
    if email:
        mud["email"] = email
    if tipo == "TERCEIROS":
        for c in ("uf", "municipio", "bairro", "logradouro", "numero", "complemento", "cep"):
            mud[c] = (endereco or {}).get(c, "")
    # Validacao/conflito acontece aqui; se falhar, nada abaixo roda.
    novo = tc.atualizar(conn, transportadora_id, mud, por)
    agora = _agora()
    conn.execute(
        "UPDATE transportadora_consultas SET respondido_em=?, respondido_por=?, canal_resposta=? "
        "WHERE transportadora_id=? AND respondido_em IS NULL", (agora, por, canal, transportadora_id))
    conn.execute(
        "UPDATE transportadora_pedidos_parados SET liberado_em=? "
        "WHERE transportadora_id=? AND liberado_em IS NULL", (agora, transportadora_id))
    conn.commit()
    return novo


def deve_avisar_hoje(consulta: dict, hoje: date) -> bool:
    d = hoje.isoformat()
    return not any((consulta.get(c) or "")[:10] == d for c in ("enviado_em", "ultimo_lembrete_em"))


def gerar_token(secret: str, consulta_id: int) -> str:
    return URLSafeTimedSerializer(secret).dumps({"c": consulta_id}, salt=_SALT)


def ler_token(secret: str, token: str, validade_dias: int) -> int | None:
    """id da consulta; None se invalido; -1 se expirado."""
    try:
        dados = URLSafeTimedSerializer(secret).loads(token, salt=_SALT, max_age=int(validade_dias) * 86400)
    except SignatureExpired:
        return -1
    except BadSignature:
        return None
    try:
        return int(dados["c"])
    except (KeyError, TypeError, ValueError):
        return None


def consultas_abertas(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, t.nome AS transportadora_nome, "
        "(SELECT COUNT(*) FROM transportadora_pedidos_parados p WHERE p.transportadora_id=c.transportadora_id "
        " AND p.embarcador_cnpj=c.embarcador_cnpj AND p.liberado_em IS NULL) AS pedidos_parados "
        "FROM transportadora_consultas c JOIN transportadoras t ON t.id=c.transportadora_id "
        "WHERE c.respondido_em IS NULL ORDER BY c.id")
    return [dict(r) for r in rows]


def config_consulta(config: dict) -> dict:
    cfg = (config or {}).get("consulta_transportadora") or {}
    return {"ativo": bool(cfg.get("ativo", False)),
            "validade_dias": int(cfg.get("validade_dias") or 30),
            "forcar_destino_email": cfg.get("forcar_destino_email"),
            "forcar_destino_whatsapp": cfg.get("forcar_destino_whatsapp")}


def link(config: dict, consulta_id: int) -> str:
    pc = (config or {}).get("portal_cliente") or {}
    base = (pc.get("url_base") or "https://app.freshhub.com.br/cliente").rstrip("/")
    return f"{base}/transportadora/{gerar_token(pc['secret_key'], consulta_id)}"


def _embarcador(conn, embarcador_cnpj: str) -> dict:
    """Contato do embarcador na tabela interno ('stkkc:<id>' busca pelo id)."""
    vazio = {"emails": [], "whatsapp": None}
    chave = str(embarcador_cnpj or "")
    por_id = chave.startswith("stkkc:")
    try:
        row = conn.execute(
            "SELECT email, notificar_email FROM interno WHERE "
            + ("stkkc_id=?" if por_id else "cnpj_embarcador=?"),
            (chave[6:] if por_id else chave,)).fetchone()
    except sqlite3.OperationalError:
        return vazio
    if not row:
        return vazio
    emails = []
    if row[1] is None or row[1]:
        emails = [e.strip() for e in re.split(r"[,;\t]+", str(row[0] or "")) if e.strip() and "@" in e]
    zap = None if por_id else preferencias_notificacao.whatsapp_do_embarcador(conn, chave)
    return {"emails": emails, "whatsapp": zap}


def _corpo_email(transp: dict, pedidos: list[dict], url: str, lembrete: bool) -> str:
    e = html.escape
    borda = f"padding:6px 10px;border-bottom:1px solid {email_utils.COR_BORDA};font-size:13px;"
    linhas = ""
    for p in pedidos:
        r = p["resumo"] or {}
        cidade = str(r.get("cidade") or "-") + (f" - {r['uf']}" if r.get("uf") else "")
        linhas += (f'<tr><td style="{borda}">{e(p["pedido"])}</td>'
                   f'<td style="{borda}">{e(str(r.get("destinatario") or "-"))}</td>'
                   f'<td style="{borda}">{e(cidade)}</td></tr>')
    cnpj = transp.get("cnpj") or ""
    cnpj_txt = f" (CNPJ {e(cnpj)})" if cnpj else ""
    topo = "Lembrete: " if lembrete else ""
    conteudo = f"""
    <p style="margin:0 0 4px 0;font-size:20px;font-weight:700;color:{email_utils.COR_PRIMARIA};">{topo}Precisamos de uma informação sobre a transportadora</p>
    <p style="margin:0 0 20px 0;font-size:14px;color:{email_utils.COR_TEXTO_SUAVE};">
      Não conhecemos a transportadora <strong>{e(transp.get("nome") or "")}</strong>{cnpj_txt}.
    </p>
    <p style="margin:0 0 12px 0;font-size:14px;color:{email_utils.COR_TEXTO};line-height:1.6;">
      <strong>Os pedidos abaixo continuam parados</strong> e só serão roteirizados depois da sua resposta.
    </p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid {email_utils.COR_BORDA};border-radius:8px;margin-bottom:20px;">
      <tr style="background-color:{email_utils.COR_PRIMARIA_CLARA};">
        <th align="left" style="padding:8px 10px;font-size:12px;">Pedido</th>
        <th align="left" style="padding:8px 10px;font-size:12px;">Destinatário</th>
        <th align="left" style="padding:8px 10px;font-size:12px;">Cidade</th></tr>
      {linhas}
    </table>
    <p style="margin:0 0 8px 0;font-size:14px;color:{email_utils.COR_TEXTO};">Basta escolher uma das opções:</p>
    <ul style="margin:0 0 20px 0;padding-left:20px;font-size:14px;color:{email_utils.COR_TEXTO};line-height:1.7;">
      <li><strong>Retirada no galpão:</strong> a transportadora busca a mercadoria na Freshlog.</li>
      <li><strong>Redespacho com endereço:</strong> levamos até o endereço da transportadora que você informar.</li>
      <li><strong>Entregar no endereço do pedido:</strong> a Freshlog entrega direto ao destinatário.</li>
    </ul>
    <p style="margin:0 0 24px 0;"><a href="{e(url)}" style="display:inline-block;background-color:{email_utils.COR_PRIMARIA};color:#FFFFFF;text-decoration:none;font-weight:600;font-size:14px;padding:12px 24px;border-radius:6px;">Informar como a transportadora atende</a></p>
    <p style="margin:0;font-size:14px;color:{email_utils.COR_TEXTO};">Atenciosamente,<br><strong>Freshlog Logística</strong></p>
    """
    return email_utils.envelope_html(conteudo, cor_acento=email_utils.COR_DESTAQUE)


def enviar_aviso(conn, consulta: dict, config: dict, lembrete: bool, modo_teste: bool) -> dict:
    """Avisa o embarcador (e-mail + WhatsApp) com o link de resposta.
    Marca enviado_em (1o aviso) ou ultimo_lembrete_em; modo_teste monta tudo
    mas nao envia nem marca."""
    res = {"email": False, "whatsapp": "desligado", "sem_contato": False}
    cfg = config_consulta(config)
    if not cfg["ativo"]:
        return res
    transp = tc.buscar(conn, consulta["transportadora_id"]) or {}
    pedidos = pedidos_parados(conn, consulta["transportadora_id"], consulta["embarcador_cnpj"])
    emb = _embarcador(conn, consulta["embarcador_cnpj"])
    forcar = (cfg["forcar_destino_email"] or "").strip()
    destinos = [forcar] if forcar else emb["emails"]
    if not destinos and not emb["whatsapp"]:
        res["sem_contato"] = True
        return res
    url = link(config, consulta["id"])
    nome = transp.get("nome") or ""
    if destinos:
        assunto = f"Fresh Log: pedidos parados aguardando informação da transportadora {nome}"
        if lembrete:
            assunto = "Lembrete - " + assunto
        corpo = _corpo_email(transp, pedidos, url, lembrete)
        if modo_teste:
            logger.info(f"[modo-teste] e-mail da consulta {consulta['id']} iria para {destinos}")
        else:
            res["email"] = bool(email_utils.enviar_email(destinos, assunto, corpo, (config or {}).get("email") or {}))
    if emb["whatsapp"]:
        texto = notificar_whatsapp.texto_consulta_transportadora(nome, len(pedidos), url, lembrete)
        assinatura = f"consulta_transportadora:{consulta['id']}:{date.today().isoformat()}"
        res["whatsapp"] = notificar_whatsapp.avisar_cliente_transportadora(
            consulta["embarcador_cnpj"], emb["whatsapp"], texto, assinatura, config, modo_teste=modo_teste)
    if not modo_teste and (res["email"] or res["whatsapp"] == "enviado"):
        campo = "ultimo_lembrete_em" if lembrete else "enviado_em"
        conn.execute(f"UPDATE transportadora_consultas SET {campo}=? WHERE id=?", (_agora(), consulta["id"]))
        conn.commit()
    return res
