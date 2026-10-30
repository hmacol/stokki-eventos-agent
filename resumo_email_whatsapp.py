# -*- coding: utf-8 -*-
"""
resumo_email_whatsapp.py

Leva pro WhatsApp do Hugo o resumo diario do Gmail (pedido dele, 28/10/2026).

Quem escreve o resumo e uma rotina na nuvem (claude.ai/code/routines,
"Resumo diario do Gmail (7h)"): as 07h ela le o Gmail e manda pra
hugo@freshlogbr.com o e-mail "Resumo do e-mail · DD/MM", com o corpo ja no
formato do WhatsApp. A rotina nao alcanca o OpenWA (so escuta dentro da
VPS), por isso este script busca o e-mail do dia por IMAP e encaminha o
corpo pro grupo whatsapp_notificacoes.grupo_resumo_email_id. So vale e-mail
enviado pela propria conta (in:sent + From = email.remetente).

Roda por timer de 15 em 15 min das 07h15 as 08h45
(infra/stokki-resumo-email-whatsapp.timer). Um envio por dia: a assinatura
resumo_email:<data> em notificacoes_whatsapp barra as rodadas seguintes.
E-mail ainda nao chegou = nada acontece, a proxima rodada tenta de novo.

Uso:
    python resumo_email_whatsapp.py [--modo-teste]
"""
import argparse
import email
import imaplib
import logging
import re
import sys
from datetime import date, datetime
from email.utils import parseaddr
from pathlib import Path

import yaml

_RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(_RAIZ))

import notificar_whatsapp as nw  # noqa: E402
from email_leitura_utils import decodificar_header, extrair_texto_corpo, fetch_em_lote  # noqa: E402

logger = logging.getLogger("resumo_email_whatsapp")

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
ASSUNTO = "Resumo do e-mail"


def escolher_resumo(mensagens: list[tuple[str, str]], hoje: date) -> str | None:
    """`mensagens` = [(assunto, corpo)] na ordem do IMAP (mais antiga
    primeiro). Devolve o corpo do resumo de HOJE mais recente, ou None."""
    marca = hoje.strftime("%d/%m")
    for assunto, corpo in reversed(mensagens):
        if ASSUNTO.lower() in assunto.lower() and marca in assunto and corpo.strip():
            return corpo
    return None


def _pasta_todos(mail) -> str:
    """'Todos os e-mails' tem nome traduzido; acha pela flag \\All. Sem ela,
    INBOX (o e-mail de si pra si cai na caixa de entrada)."""
    status, pastas = mail.list()
    for linha in (pastas or []) if status == "OK" else []:
        texto = linha.decode(errors="ignore") if isinstance(linha, bytes) else str(linha)
        if "\\All" in texto:
            nome = re.search(r'"([^"]+)"\s*$', texto)
            if nome:
                return f'"{nome.group(1)}"'
    return "INBOX"


def buscar_resumo_do_dia(cfg_email: dict, hoje: date) -> str | None:
    usuario, senha = cfg_email.get("remetente", ""), cfg_email.get("senha_app", "")
    if not usuario or not senha:
        logger.warning("IMAP desativado: remetente/senha_app nao configurados em config.yaml.")
        return None
    mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=30)
    try:
        mail.login(usuario, senha)
        mail.select(_pasta_todos(mail), readonly=True)
        # So o que a PROPRIA conta enviou (in:sent): e-mail de fora com o mesmo
        # assunto nao pode virar mensagem no WhatsApp do Hugo.
        status, dados = mail.search(None, "X-GM-RAW", '"in:sent from:me subject:resumo newer_than:2d"')
        ids = dados[0].split() if status == "OK" and dados and dados[0] else []
        mensagens = []
        for entrada in fetch_em_lote(mail, ids, "(BODY.PEEK[])").values():
            msg = email.message_from_bytes(entrada[1])
            if parseaddr(msg.get("From", ""))[1].lower() != usuario.lower():
                continue
            mensagens.append((decodificar_header(msg.get("Subject", "")), extrair_texto_corpo(msg)))
        return escolher_resumo(mensagens, hoje)
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Resumo diario do Gmail no WhatsApp do Hugo.")
    parser.add_argument("--modo-teste", action="store_true", help="so mostra o que enviaria")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    grupo = (config.get("whatsapp_notificacoes") or {}).get("grupo_resumo_email_id")
    if not args.modo_teste and not (nw.gateway_clientes_ok(config) and grupo):
        logger.info("WhatsApp desligado ou sem whatsapp_notificacoes.grupo_resumo_email_id: nada a fazer.")
        return 0
    hoje = datetime.now().date()
    corpo = buscar_resumo_do_dia(config.get("email") or {}, hoje)
    if not corpo:
        logger.info(f"Resumo de {hoje:%d/%m} ainda nao chegou no Gmail.")
        return 0
    situacao = nw.avisar_resumo_email(corpo, hoje, config, modo_teste=args.modo_teste)
    logger.info(f"Resumo de {hoje:%d/%m}: {situacao}.")
    if args.modo_teste:
        print(nw.texto_resumo_email(corpo))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
