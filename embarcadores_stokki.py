# -*- coding: utf-8 -*-
"""
embarcadores_stokki.py

Embarcador novo na Stokki vira pre-cadastro em `interno` e recebe convite
pra completar o cadastro no portal (pedido do Hugo, 07/10/2026; spec em
docs/superpowers/specs/2026-10-08-embarcadores-novos-stokki-design.md).

Roda de hora em hora, 07h-19h (infra/stokki-embarcadores-novos.timer):
1. le o select de clientes da Stokki e compara com interno.stkkc_id;
2. novo: linha em interno (sem habilidade) + cadastro_embarcador;
3. ativo (pedido nos ultimos 30 dias): sender da Vuupt (o do pedido mais
   antigo no nucleo, ou cria o customer) e convite: WhatsApp do celular da
   Stokki, senao e-mail, senao so aviso no grupo;
4. convite sem PIN depois de 3 dias: reenvia uma vez.

Envio ao cliente preso por padrao: WhatsApp em
whatsapp_notificacoes.clientes.forcar_destino; e-mail em
cadastro_embarcador.forcar_destino_email (ausente = hugo@).

Uso:
    python embarcadores_stokki.py [--modo-teste]
"""
import argparse
import html as html_lib
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "portal_cliente"))

import auth_cliente as auth  # noqa: E402  (portal_cliente/auth_cliente.py)
import cadastro_embarcador as cad  # noqa: E402
import notificar_whatsapp as nw  # noqa: E402
import preferencias_notificacao as pn  # noqa: E402

logger = logging.getLogger("embarcadores_stokki")

_RE_ROTULO = re.compile(r"^(?P<apelido>.*?)\s*\((?P<razao>.*)\s+-\s+(?P<cnpj>[\d./-]{14,18})\)\s*$")


def interpretar_rotulo(select_label: str, name: str) -> tuple[str | None, str, str]:
    """'APELIDO (RAZAO - 00.000.000/0000-00)' -> (apelido, razao, cnpj).
    Apelido igual a razao vira None. Fora do padrao: (None, name, '')."""
    m = _RE_ROTULO.match(str(select_label or "").strip())
    if not m:
        return None, str(name or "").strip(), ""
    razao = m.group("razao").strip()
    apelido = m.group("apelido").strip()
    cnpj = re.sub(r"\D", "", m.group("cnpj"))
    if len(cnpj) != 14:
        return None, razao or str(name or "").strip(), ""
    if not apelido or apelido.upper() == razao.upper():
        apelido = None
    return apelido, razao, cnpj


def clientes_do_select(bruto) -> list[dict]:
    """Resposta de stokki.pedidos.listar_embarcadores -> lista de clientes."""
    itens = bruto.get("clients", []) if isinstance(bruto, dict) else (bruto or [])
    clientes = []
    for c in itens:
        apelido, razao, cnpj = interpretar_rotulo(c.get("select_label", ""), c.get("name", ""))
        clientes.append({"stkkc_id": int(c["id"]), "apelido": apelido, "razao": razao, "cnpj": cnpj})
    return clientes


def _valor_input(html: str, nome: str) -> str:
    m = re.search(r'<input[^>]*\bname="%s"[^>]*>' % re.escape(nome), html or "")
    if not m:
        return ""
    v = re.search(r'\bvalue="([^"]*)"', m.group(0))
    return html_lib.unescape(v.group(1)).strip() if v else ""


def extrair_contato(html: str) -> dict:
    """Tela /administrator/client/edit/{id} da Stokki -> contato. Pessoa
    primeiro, empresa como reserva."""
    return {
        "celular": _valor_input(html, "cellphone") or _valor_input(html, "company_cellphone"),
        "telefone": _valor_input(html, "phone") or _valor_input(html, "company_phone"),
        "email": (_valor_input(html, "email") or _valor_input(html, "company_email[]")).lower(),
        "cidade": _valor_input(html, "city"),
    }


def celular_whatsapp(valor) -> str:
    """Numero que vale tentar no WhatsApp, ou ''. Recusa invalido e numero de
    mentira com o mesmo digito repetido, ex. (11) 999 999 999."""
    try:
        numero = pn.normalizar_whatsapp(valor)
    except ValueError:
        return ""
    if not numero or len(set(numero[4:])) == 1:
        return ""
    return numero


DIAS_ATIVO = 30
_SQL_CNPJ_REMETENTE = "REPLACE(REPLACE(REPLACE(remetente_codigo, '.', ''), '/', ''), '-', '')"


def _fmt(agora: datetime) -> str:
    return agora.strftime("%Y-%m-%d %H:%M:%S")


def _tabela_existe(conn, nome: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)).fetchone() is not None


def tem_pedido_recente(conn, cnpj: str, agora: datetime, dias: int = DIAS_ATIVO) -> bool:
    if not _tabela_existe(conn, "nucleo_pedidos"):
        return False
    limite = _fmt(agora - timedelta(days=dias))
    return conn.execute(f"SELECT 1 FROM nucleo_pedidos WHERE {_SQL_CNPJ_REMETENTE} = ? AND criado_em >= ? LIMIT 1",
                        (cnpj, limite)).fetchone() is not None


def sender_do_nucleo(conn, cnpj: str) -> int | None:
    """Sender do pedido mais antigo desse remetente: e o que a Vuupt criou
    primeiro; os pedidos seguintes passam a apontar pra ele via interno."""
    if not _tabela_existe(conn, "nucleo_pedidos"):
        return None
    row = conn.execute(f"SELECT sender_id FROM nucleo_pedidos WHERE {_SQL_CNPJ_REMETENTE} = ? AND sender_id IS NOT NULL "
                       "ORDER BY criado_em, codigo LIMIT 1", (cnpj,)).fetchone()
    return int(row[0]) if row else None


def resolver_sender(conn, cnpj: str, razao: str, vuupt) -> int | None:
    sender = sender_do_nucleo(conn, cnpj)
    if sender or vuupt is None:
        return sender
    try:
        resposta = vuupt.criar_customer({"name": razao, "code": auth.formatar_cnpj(cnpj)})
    except Exception as exc:
        logger.warning(f"Falha ao criar o remetente de {cnpj} na Vuupt: {exc}")
        return None
    dados = resposta.get("customer", resposta) if isinstance(resposta, dict) else {}
    return int(dados["id"]) if isinstance(dados, dict) and dados.get("id") else None


def separar(clientes: list[dict], conn) -> dict:
    """novos: CNPJ fora de interno; sem_stkkc: CNPJ em interno sem stkkc_id
    (cadastro manual antigo); conflitos: CNPJ em interno com outro stkkc_id;
    ignorados: rotulo sem CNPJ."""
    por_stkkc, por_cnpj = set(), {}
    for cnpj, stkkc in conn.execute("SELECT cnpj_embarcador, stkkc_id FROM interno").fetchall():
        if stkkc is not None:
            por_stkkc.add(int(stkkc))
        por_cnpj[re.sub(r"\D", "", str(cnpj or ""))] = stkkc
    g = {"novos": [], "sem_stkkc": [], "conflitos": [], "ignorados": []}
    for cli in clientes:
        if cli["stkkc_id"] in por_stkkc:
            continue
        if not cli["cnpj"]:
            g["ignorados"].append(cli)
        elif cli["cnpj"] not in por_cnpj:
            g["novos"].append(cli)
        elif por_cnpj[cli["cnpj"]] is None:
            g["sem_stkkc"].append(cli)
        else:
            g["conflitos"].append((cli, int(por_cnpj[cli["cnpj"]])))
    return g


def pre_cadastrar(conn, cli: dict, contato: dict, ativo: bool, agora: datetime) -> None:
    momento = _fmt(agora)
    obs = f"Pré-cadastro automático {agora:%d/%m/%Y} (Stokki #stkkc-{cli['stkkc_id']})"
    if not ativo:
        obs += ", inativo: sem pedido nos últimos 30 dias"
    conn.execute(
        "INSERT INTO interno (cnpj_embarcador, stkkc_id, nome_remetente, apelido, email, fator_ponderado, habilidade, "
        "notificar_email, observacoes, criado_em, atualizado_em) VALUES (?, ?, ?, ?, ?, 1.0, NULL, ?, ?, ?, ?)",
        (cli["cnpj"], cli["stkkc_id"], cli["razao"], cli["apelido"], contato["email"] or None,
         1 if ativo else 0, obs, momento, momento))
    conn.commit()
    cad.registrar(conn, cli["cnpj"], cli["stkkc_id"], cad.PENDENTE if ativo else cad.INATIVO,
                  celular=contato["celular"], email=contato["email"], agora=agora)
