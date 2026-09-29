# -*- coding: utf-8 -*-
"""
integracao_openwa.py

Envio de WhatsApp pelo gateway OpenWA (github.com/rmyndharis/OpenWA,
auto-hospedado, nao-oficial) -- usado SO pelas notificacoes internas
(notificar_whatsapp.py). Nao tem relacao com a central de atendimento,
que usa a Evolution API (integracao_evolution.py).

O numero pareado e o do proprio Hugo (decisao dele, 28/09/2026, risco de
banimento aceito). Por isso o volume e controlado em notificar_whatsapp.py
e aqui nao existe reenvio.

config.yaml (nao versionado):
    whatsapp_notificacoes:
        base_url: "http://127.0.0.1:2785/api"   # gateway nunca e exposto pelo Caddy
        api_key: "..."                          # chave de papel operator
        sessao: "..."                           # id (UUID) da sessao no gateway
"""
import logging

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def configurado(cfg: dict | None) -> bool:
    if not cfg:
        return False
    return all(cfg.get(chave) for chave in ("base_url", "api_key", "sessao"))


def enviar_texto(cfg: dict, chat_id: str, texto: str) -> tuple[bool, str | None]:
    """POST /sessions/{sessao}/messages/send-text. `chat_id` no formato do
    WhatsApp ('<numero>@g.us' pra grupo, '<telefone>@c.us' pra pessoa).
    Nunca levanta excecao -- qualquer falha vira (False, None). Retorna
    (sucesso, id da mensagem no gateway)."""
    if not configurado(cfg) or not chat_id or not texto:
        return False, None
    url = f"{cfg['base_url'].rstrip('/')}/sessions/{cfg['sessao']}/messages/send-text"
    try:
        resp = requests.post(url, json={"chatId": chat_id, "text": texto},
                             headers={"X-API-Key": cfg["api_key"]}, timeout=_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(f"Falha ao enviar WhatsApp pelo OpenWA: {exc}")
        return False, None
    try:
        dados = resp.json()
    except ValueError:
        dados = None
    return True, dados.get("messageId") if isinstance(dados, dict) else None
