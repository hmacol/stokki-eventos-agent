# -*- coding: utf-8 -*-
"""
integracao_evolution.py

Envio de WhatsApp via Evolution API (gateway auto-hospedado, protocolo
WhatsApp Web/Baileys -- não-oficial, risco de banimento aceito pelo Hugo
em 26/08 pela inviabilidade financeira da WhatsApp Cloud API/Chatwoot).
Substitui integracao_chatwoot.py -- mesma assinatura pública de
enviar_whatsapp_oferta(), usado por
roteirizacao/avisar_motoristas_rotas.py -- e é a mesma primitiva
enviar_texto() usada pela rota de resposta da central de atendimento
(atendimento/app.py).

Diferença importante em relação ao Chatwoot/Cloud API: não existe janela
de 24h nem exigência de template aprovado -- Baileys manda texto livre a
qualquer momento (essa restrição é regra da Cloud API oficial, não do
protocolo WhatsApp Web).

O formato exato do payload de POST /message/sendText segue a documentação
pública da Evolution API v2 até o momento desta implementação -- **ainda
não testado contra a instância real** (só dá pra validar de fato depois
que a instância estiver no ar e o número pareado). Se o envio falhar por
causa de mudança de formato, checar a resposta de erro da Evolution API
antes de mexer em qualquer outra coisa -- é o primeiro lugar a olhar
(mesma cautela que já existia no integracao_chatwoot.py).

Credenciais esperadas em config.yaml (novas, não versionadas):
    evolution_api:
        base_url: "http://127.0.0.1:8080"   # interno -- Evolution nunca é exposta pelo Caddy
        api_key: "..."                      # AUTHENTICATION_API_KEY da instância
        instance: "..."                     # nome da instância criada (POST /instance/create)
        webhook_secret: "..."               # segredo no path do webhook (/webhook/evolution/<segredo>)
"""
import logging
import re

import requests

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_TIMEOUT_MIDIA = 20  # mídia demora mais que texto pra baixar, mas não pode travar o webhook por muito tempo


def configurado(cfg: dict | None) -> bool:
    """True só quando as 3 chaves necessárias estão presentes -- quem
    chama usa isso pra decidir se vale a pena tentar, sem precisar
    tratar exceção só pra descobrir que a integração nem está pronta."""
    if not cfg:
        return False
    return all(cfg.get(chave) for chave in ("base_url", "api_key", "instance"))


def _telefone_e164(telefone: str) -> str | None:
    """Normaliza pra E.164 assumindo Brasil (+55) quando o DDI não vem no
    cadastro -- mesma regra de integracao_chatwoot.py."""
    digitos = re.sub(r"\D", "", telefone or "")
    if not digitos:
        return None
    if not digitos.startswith("55"):
        digitos = "55" + digitos
    return f"+{digitos}"


def enviar_texto(cfg: dict, telefone: str, texto: str) -> tuple[bool, str | None]:
    """Primitiva única de envio, reaproveitada pelo aviso automático de
    oferta de rota (enviar_whatsapp_oferta) e pela rota de resposta da
    central de atendimento. Nunca levanta exceção -- qualquer falha
    (rede, credencial, número sem WhatsApp etc.) vira (False, None).
    Retorna (sucesso, evolution_message_id) -- o id serve pra já gravar
    a mensagem em atendimento/banco.py com o mesmo id que vai chegar de
    volta no webhook (evita duplicar quando o eco da própria mensagem
    enviada retornar via messages.upsert)."""
    if not configurado(cfg):
        return False, None
    telefone_e164 = _telefone_e164(telefone)
    if not telefone_e164:
        return False, None

    base = cfg["base_url"].rstrip("/")
    instancia = cfg["instance"]
    headers = {"apikey": cfg["api_key"]}

    try:
        resp = requests.post(
            f"{base}/message/sendText/{instancia}",
            json={"number": telefone_e164.lstrip("+"), "text": texto},
            headers=headers, timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        dados = resp.json()
        message_id = (dados.get("key") or {}).get("id")
        return True, message_id
    except requests.RequestException as exc:
        logger.warning(f"Falha ao enviar WhatsApp via Evolution API para {telefone}: {exc}")
        return False, None


def baixar_midia(cfg: dict, mensagem_bruta: dict) -> dict | None:
    """Busca o conteúdo (base64) de uma mensagem de mídia recebida --
    POST /chat/getBase64FromMediaMessage/{instance}. `mensagem_bruta` precisa
    ser o objeto INTEIRO do evento messages.upsert (key + message juntos,
    não só a key sozinha) -- confirmado direto no código-fonte da Evolution
    API (whatsapp.baileys.service.ts::getBase64FromMediaMessage): se faltar
    o `message`, ela tenta reconsultar do armazenamento interno do Baileys,
    que pode não ter mais a mensagem. Por isso o download tem que acontecer
    no momento do webhook, com o payload completo ainda em mãos.

    Nunca levanta exceção -- retorna None em qualquer falha (mídia expirada,
    rede, resposta sem base64 etc.), mesmo padrão de enviar_texto."""
    if not configurado(cfg):
        return None
    base = cfg["base_url"].rstrip("/")
    instancia = cfg["instance"]
    headers = {"apikey": cfg["api_key"]}
    try:
        resp = requests.post(
            f"{base}/chat/getBase64FromMediaMessage/{instancia}",
            json={"message": mensagem_bruta}, headers=headers, timeout=_TIMEOUT_MIDIA,
        )
        resp.raise_for_status()
        dados = resp.json()
        if not dados.get("base64"):
            return None
        return dados
    except requests.RequestException as exc:
        logger.warning(f"Falha ao baixar mídia via Evolution API: {exc}")
        return None
    except ValueError as exc:  # resposta não é JSON válido
        logger.warning(f"Resposta inesperada ao baixar mídia via Evolution API: {exc}")
        return None


def enviar_whatsapp_oferta(cfg: dict, telefone: str, nome: str, link_escolha: str) -> bool:
    """Mesma assinatura de integracao_chatwoot.enviar_whatsapp_oferta --
    só troca o transporte. Sem regra de janela de 24h/template."""
    texto = (
        f"Você tem rota(s) disponível(is) para escolha, {nome}: {link_escolha}"
    )
    sucesso, _id = enviar_texto(cfg, telefone, texto)
    return sucesso


# ── Administração da instância (tela /admin/whatsapp de atendimento/app.py) ──
# Usado só pelo admin da central de atendimento pra parear/checar o número --
# nunca pelo fluxo de mensagens em si.

def status_instancia(cfg: dict) -> dict:
    """GET /instance/connectionState/{instance}. Levanta em caso de falha
    de rede/config -- quem chama (rota admin) decide como mostrar o erro
    na tela, diferente de enviar_texto que nunca deve travar um fluxo
    automático."""
    base = cfg["base_url"].rstrip("/")
    resp = requests.get(
        f"{base}/instance/connectionState/{cfg['instance']}",
        headers={"apikey": cfg["api_key"]}, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def gerar_pareamento(cfg: dict, numero: str | None = None) -> dict:
    """GET /instance/connect/{instance}, opcionalmente com ?number= pra
    pedir código de pareamento por telefone em vez de QR code (não exige
    câmera perto da VPS -- o Hugo digita o código direto no WhatsApp do
    celular, em Aparelhos conectados > Conectar com número de telefone).
    Retorna o payload cru da Evolution API (contém "code"/"base64" ou
    "pairingCode", dependendo da versão/modo) -- o template decide o que
    mostrar."""
    base = cfg["base_url"].rstrip("/")
    params = {"number": re.sub(r"\D", "", numero)} if numero else None
    resp = requests.get(
        f"{base}/instance/connect/{cfg['instance']}",
        headers={"apikey": cfg["api_key"]}, params=params, timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
