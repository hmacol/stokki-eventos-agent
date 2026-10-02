# -*- coding: utf-8 -*-
"""
integracao_openwa.py

Envio de WhatsApp pelo gateway OpenWA (github.com/rmyndharis/OpenWA,
auto-hospedado, nao-oficial) -- usado pelas notificacoes internas
(notificar_whatsapp.py) e pelo aviso manual aos clientes sobre pedidos fora
da area (avisar_fora_area.py). Nao tem relacao com a central de atendimento,
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


def listar_grupos(cfg: dict) -> list[dict] | None:
    """GET /sessions/{sessao}/groups: grupos de que o numero participa, como
    [{"id": "...@g.us", "nome": "..."}] ordenados por nome. None quando nao
    configurado ou quando o gateway nao responde (quem chama avisa que o
    gateway esta fora do ar). Nunca levanta excecao."""
    if not configurado(cfg):
        return None
    url = f"{cfg['base_url'].rstrip('/')}/sessions/{cfg['sessao']}/groups"
    try:
        resp = requests.get(url, headers={"X-API-Key": cfg["api_key"]}, timeout=_TIMEOUT)
        resp.raise_for_status()
        dados = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Falha ao listar grupos no OpenWA: {exc}")
        return None
    if not isinstance(dados, list):
        return None
    grupos = [{"id": g["id"], "nome": g["name"]} for g in dados
              if isinstance(g, dict) and g.get("id") and g.get("name")]
    return sorted(grupos, key=lambda g: g["nome"].lower())


def numero_existe(cfg: dict, numero: str) -> bool | None:
    """GET /sessions/{sessao}/contacts/check/{numero}: o numero tem conta no
    WhatsApp? O send-text devolve 201 mesmo pra numero que nao existe, por
    isso a consulta antes de falar com um cliente (nono digito etc.).
    True/False pela resposta; None quando o gateway nao soube responder
    (503, sessao fora, erro de rede) -- quem chama decide se tenta depois."""
    if not configurado(cfg) or not numero:
        return None
    url = f"{cfg['base_url'].rstrip('/')}/sessions/{cfg['sessao']}/contacts/check/{numero}"
    try:
        resp = requests.get(url, headers={"X-API-Key": cfg["api_key"]}, timeout=_TIMEOUT)
        resp.raise_for_status()
        dados = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning(f"Falha ao consultar numero no OpenWA: {exc}")
        return None
    if not isinstance(dados, dict) or "exists" not in dados:
        return None
    return bool(dados["exists"])
