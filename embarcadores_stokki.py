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
