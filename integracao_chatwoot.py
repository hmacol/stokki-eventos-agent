# -*- coding: utf-8 -*-
"""
integracao_chatwoot.py

Envio de WhatsApp via Chatwoot (atendimento.freshhub.com.br, WhatsApp
Cloud API oficial já conectado -- ver memória project_atendimento_unificado)
para o aviso automático de oferta de rota no marketplace (Hugo, 22/08).

IMPORTANTE -- duas dependências fora do código, fora do meu controle:
  1. Verificação de CNPJ da Meta aprovada (estava "em revisão" em 19/08).
  2. Um TEMPLATE de mensagem aprovado pela Meta pra este caso de uso --
     fora da janela de 24h (o motorista não escreveu primeiro), a Cloud
     API só aceita mensagem de TEMPLATE pré-aprovado, nunca texto livre.
Sem as duas, toda chamada aqui falha (ou nem deveria ser tentada -- ver
`configurado()`) e quem chama (roteirizacao/avisar_motoristas_rotas.py)
cai automaticamente no fallback de copiar/colar pro WhatsApp.

O formato exato do payload de envio de mensagem de template abaixo
segue a API pública do Chatwoot documentada até o momento desta
implementação -- **ainda não testado contra a instância real** (só dá
pra validar de fato depois que o template estiver aprovado na Meta e o
account_id/inbox_id/token reais estiverem em config.yaml). Se o envio
falhar por causa de mudança de formato, checar a resposta de erro do
Chatwoot antes de mexer em qualquer outra coisa -- é o primeiro lugar a
olhar.

Credenciais esperadas em config.yaml (novas, não versionadas):
    chatwoot:
        base_url: "https://atendimento.freshhub.com.br"
        account_id: <int>
        inbox_id: <int>              # inbox do canal WhatsApp
        api_access_token: "..."      # token de agente/API do Chatwoot
        template_oferta_rota: "..."  # nome do template aprovado na Meta
"""
import logging
import re

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15


def configurado(cfg: dict | None) -> bool:
    """True só quando as 5 chaves necessárias estão presentes -- quem
    chama usa isso pra decidir se vale a pena tentar, sem precisar
    tratar exceção só pra descobrir que a integração nem está pronta."""
    if not cfg:
        return False
    return all(cfg.get(chave) for chave in
               ("base_url", "account_id", "inbox_id", "api_access_token", "template_oferta_rota"))


def _telefone_e164(telefone: str) -> str | None:
    """Normaliza pra E.164 assumindo Brasil (+55) quando o DDI não vem
    no cadastro -- formato que a Cloud API exige pro telefone do
    contato."""
    digitos = re.sub(r"\D", "", telefone or "")
    if not digitos:
        return None
    if not digitos.startswith("55"):
        digitos = "55" + digitos
    return f"+{digitos}"


def _buscar_ou_criar_contato(cfg: dict, headers: dict, telefone_e164: str, nome: str) -> int | None:
    base = cfg["base_url"].rstrip("/")
    conta = cfg["account_id"]

    resp = requests.get(
        f"{base}/api/v1/accounts/{conta}/contacts/search",
        params={"q": telefone_e164}, headers=headers, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    encontrados = resp.json().get("payload", [])
    if encontrados:
        return encontrados[0]["id"]

    resp = requests.post(
        f"{base}/api/v1/accounts/{conta}/contacts",
        json={"name": nome, "phone_number": telefone_e164}, headers=headers, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("payload", {}).get("contact", {}).get("id")


def enviar_whatsapp_oferta(cfg: dict, telefone: str, nome: str, link_escolha: str) -> bool:
    """Envia (via template aprovado) o aviso de que há rota(s)
    disponível(is) pro telefone informado. Nunca levanta exceção --
    qualquer falha (rede, credencial, contato sem WhatsApp, template
    ainda não aprovado etc.) vira False, pra nunca travar a publicação
    da oferta por causa do canal automático."""
    if not configurado(cfg):
        return False
    telefone_e164 = _telefone_e164(telefone)
    if not telefone_e164:
        return False

    base = cfg["base_url"].rstrip("/")
    conta = cfg["account_id"]
    headers = {"api_access_token": cfg["api_access_token"]}

    try:
        contato_id = _buscar_ou_criar_contato(cfg, headers, telefone_e164, nome)
        if not contato_id:
            return False

        resp = requests.post(
            f"{base}/api/v1/accounts/{conta}/conversations",
            json={"source_id": telefone_e164, "inbox_id": cfg["inbox_id"], "contact_id": contato_id},
            headers=headers, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        conversa_id = resp.json()["id"]

        resp = requests.post(
            f"{base}/api/v1/accounts/{conta}/conversations/{conversa_id}/messages",
            json={
                "content": f"Você tem rota(s) disponível(is) para escolha: {link_escolha}",
                "message_type": "outgoing",
                "template_params": {
                    "name": cfg["template_oferta_rota"],
                    "category": "UTILITY",
                    "language": "pt_BR",
                    "processed_params": {"1": nome, "2": link_escolha},
                },
            },
            headers=headers, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        logger.warning(f"Falha ao enviar WhatsApp via Chatwoot para {telefone}: {exc}")
        return False
