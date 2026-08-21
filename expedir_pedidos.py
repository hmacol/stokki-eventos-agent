# -*- coding: utf-8 -*-
"""
expedir_pedidos.py

Expede na Stokki os pedidos que foram entregues com sucesso no VUUPT
e possuem canhoto VALIDADO MANUALMENTE no VUUPT (validated_at preenchido).

Pedidos com canhoto pendente de validacao recebem notificacao interna.

Fluxo por pedido validado:
  1. Baixa o PDF do canhoto
  2. Expede o pedido na Stokki via POST /provider/operation/shipping/store
  3. Anexa o PDF do canhoto na aba Documentos do pedido (Playwright)

Execute:
  py -3.11 expedir_pedidos.py
  py -3.11 expedir_pedidos.py --horas 48 --limite 10
  py -3.11 expedir_pedidos.py --modo-teste
"""
import argparse
import html
import logging
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(_RAIZ / "insucesso_entrega"))

import fingerprint_expedicao
import fingerprint_duplicacao_insucesso
import fingerprint_duplicacao_agendada
import fingerprint_notificacao_interna
from motivos_falha import texto_do_motivo, aprender_motivos
from notificar_insucesso_aguardando_resposta import (
    identificar_aguardando_resposta, notificar_remetentes as notificar_insucesso_aguardando_resposta,
)
from vuupt_client import VuuptClient
import tratativas


(_RAIZ / "dados").mkdir(parents=True, exist_ok=True)
(_RAIZ / "dados" / "canhotos").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "expedicao.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("expedicao")

CONFIG_PATH  = _RAIZ / "config.yaml"
CANHOTOS_DIR = _RAIZ / "dados" / "canhotos"

VUUPT_BASE   = "https://api.vuupt.com/api/v1/services"
VUUPT_CHECK  = "https://api.vuupt.com/api/v1/checklists"
STOKKI_BASE  = "https://freshlog.stokki.com.br"
URL_SHIPPING = f"{STOKKI_BASE}/pt-br/provider/operation/shipping/store"
URL_LOGIN         = f"{STOKKI_BASE}/pt-br/administrator/login"
URL_LOGIN_PROVIDER = f"{STOKKI_BASE}/pt-br/login"
URL_PROVIDER_ADM = f"{STOKKI_BASE}/pt-br/administrator/inventory/outbound/show"
URL_PROVIDER_SHW = f"{STOKKI_BASE}/pt-br/provider/inventory/outbound/show"

HORAS_PADRAO = 168  # 7 dias (era 48h) -- validação manual no VUUPT pode demorar mais
FUSO_LOCAL   = ZoneInfo("America/Sao_Paulo")

# O e-mail de "dia sem insucesso" só sai nas execuções finais do dia
# (a tarefa de 30 em 30 min roda até 19h30) -- antes disso o dia ainda
# não terminou e um insucesso ainda pode aparecer.
HORA_EMAIL_SEM_INSUCESSO = 19


def _carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── VUUPT ─────────────────────────────────────────────────────────────────────

def _headers_vuupt(token):
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _parse_data(data_str):
    if not data_str:
        return None
    texto = data_str.strip()
    if texto.endswith("Z"):
        texto = texto[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError:
        dt = datetime.strptime(texto, "%Y-%m-%d %H:%M:%S")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extrair_checklist(servico: dict) -> dict | None:
    """Retorna o primeiro checklistAnswer do servico, ou None."""
    respostas = (servico.get("checklistAnswers") or {}).get("data") or []
    return respostas[0] if respostas else None


def tem_canhoto(servico: dict) -> bool:
    """True se o servico tem algum canhoto registrado (com ou sem validacao)."""
    cl = _extrair_checklist(servico)
    return cl is not None and int(cl.get("images_quantity") or 0) > 0


def canhoto_validado(servico: dict) -> bool:
    """
    True se o canhoto foi validado manualmente no VUUPT.
    Campo: checklistAnswers[0].validated_at (null = pendente)
    """
    cl = _extrair_checklist(servico)
    if not cl:
        return False
    return bool(cl.get("validated_at"))


def _buscar_servicos_por_status_done(token: str, status_done_alvo: str, horas: int,
                                     exigir_canhoto: bool = False) -> list:
    """
    Busca serviços com status='done' e o status_done dado ('success' ou
    'failed') dentro da janela de horas (via completed_at). Reaproveitada
    tanto por buscar_servicos_entregues (sucesso, exige canhoto) quanto
    por buscar_servicos_insucesso (falha, não exige canhoto).
    """
    limite = datetime.now(timezone.utc) - timedelta(hours=horas)
    encontrados = []
    page = 1

    while True:
        resp = requests.get(
            VUUPT_BASE,
            headers=_headers_vuupt(token),
            params={
                "page": page, "per_page": 100,
                "sort": "-completed_at",
                # failedReason: descrição oficial do motivo de insucesso,
                # usada pra aprender motivos novos automaticamente
                # (motivos_falha.aprender_motivos) -- irrelevante (null)
                # pros entregues com sucesso, não custa nada extra.
                "include": "checklistAnswers,failedReason",
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        registros = body.get("data", [])
        if not registros:
            break

        parar = False
        for s in registros:
            data = _parse_data(s.get("completed_at"))
            if data and data < limite:
                parar = True
                break
            if s.get("status") != "done" or s.get("status_done") != status_done_alvo:
                continue
            code = s.get("code", "")
            if not re.match(r"#?PS-\d+", code, re.IGNORECASE):
                continue
            if exigir_canhoto and not tem_canhoto(s):
                continue
            encontrados.append(s)

        if parar:
            break
        pag = body.get("meta", {}).get("pagination", {})
        if page >= pag.get("total_pages", page):
            break
        page += 1
        time.sleep(0.3)

    return encontrados


def buscar_servicos_entregues(token: str, horas: int = HORAS_PADRAO) -> list:
    """Busca serviços entregues com sucesso que tenham canhoto."""
    encontrados = _buscar_servicos_por_status_done(token, "success", horas, exigir_canhoto=True)
    logger.info(f"VUUPT: {len(encontrados)} entregue(s) com canhoto nas ultimas {horas}h")
    return encontrados


def buscar_servicos_insucesso(token: str, horas: int = HORAS_PADRAO) -> list:
    """
    Busca serviços com INSUCESSO na entrega (status_done='failed').

    Esses NÃO são expedidos na Stokki (decisão do Hugo, 29/07: gerar só
    um aviso pra tratamento manual, sem ação automática na Stokki) —
    diferente dos entregues com sucesso, que são expedidos normalmente.
    """
    encontrados = _buscar_servicos_por_status_done(token, "failed", horas, exigir_canhoto=False)
    logger.info(f"VUUPT: {len(encontrados)} pedido(s) com insucesso na entrega nas ultimas {horas}h")

    # Motivo de falha que ainda não está no de-para: registra a
    # descrição oficial do VUUPT automaticamente (pedido do Hugo,
    # 11/08). Só o texto -- regra de duplicação continua manual.
    novos = aprender_motivos(encontrados)
    if novos:
        logger.info(f"{novos} motivo(s) de falha novo(s) registrado(s) automaticamente.")

    return encontrados


def extrair_checklist_id(servico: dict) -> int | None:
    cl = _extrair_checklist(servico)
    return cl.get("id") if cl else None


def baixar_canhoto_pdf(token: str, checklist_id: int, codigo_ps: str) -> Path | None:
    nome    = re.sub(r"[^A-Za-z0-9_-]", "_", codigo_ps)
    destino = CANHOTOS_DIR / f"canhoto_{nome}.pdf"
    if destino.exists():
        return destino
    try:
        headers = _headers_vuupt(token)
        headers["Accept"] = "application/pdf"
        resp = requests.get(
            f"{VUUPT_CHECK}/{checklist_id}/print",
            headers=headers, timeout=30,
        )
        resp.raise_for_status()
        destino.write_bytes(resp.content)
        logger.info(f"  PDF baixado: {destino.name} ({len(resp.content)} bytes)")
        return destino
    except Exception as e:
        logger.warning(f"  Falha ao baixar PDF {checklist_id}: {e}")
        return None


# ── Notificacao interna ───────────────────────────────────────────────────────

def notificar_validacao_pendente(pendentes: list, config_email: dict, modo_teste: bool):
    """Envia e-mail interno listando pedidos com canhoto pendente de validacao."""
    if not pendentes:
        return

    remetente = config_email.get("remetente", "")
    senha     = config_email.get("senha_app") or config_email.get("senha", "")
    responsavel = config_email.get("email_responsavel", "")

    if not responsavel:
        logger.info("E-mail de responsavel nao configurado -- notificacao interna pulada.")
        for p in pendentes:
            logger.warning(f"  Validacao pendente no VUUPT: {p.get('code')}")
        return

    COR_HEADER  = "#141428"
    COR_ACENTO  = "#00C896"
    COR_DESTAQUE = "#F5A623"

    linhas = ""
    for s in pendentes:
        cl      = _extrair_checklist(s)
        filled  = (cl or {}).get("filled_at", "")[:16] if cl else ""
        linhas += (
            f"<tr>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-weight:600;"
            f"font-size:13px;color:#1F2937;'>{html.escape(s.get('code','') or '')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{html.escape(filled)}</td>"
            f"</tr>"
        )

    corpo = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
<div style="background:{COR_ACENTO};padding:20px;border-radius:8px 8px 0 0;">
<h2 style="color:#fff;margin:0;">Canhotos Pendentes de Validacao</h2></div>
<div style="background:#f9f9f9;padding:24px;border:1px solid #E5E7EB;border-top:none;border-radius:0 0 8px 8px;">
<p style="color:#1F2937;">Os pedidos abaixo foram entregues com sucesso no VUUPT e possuem canhoto
registrado, mas <strong>ainda nao foram validados manualmente</strong>. Eles nao serao expedidos
na Stokki ate que a validacao seja feita no VUUPT.</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0;">
<thead><tr style="background:{COR_HEADER};">
<th style="padding:10px 14px;text-align:left;color:#fff;font-size:12px;">Pedido</th>
<th style="padding:10px 14px;text-align:left;color:#fff;font-size:12px;">Canhoto Registrado Em</th>
</tr></thead><tbody>{linhas}</tbody></table>
<div style="background:#FFF8EC;border-left:4px solid {COR_DESTAQUE};border-radius:6px;padding:14px 18px;">
<p style="margin:0;font-size:13px;color:#1F2937;">Para liberar a expedicao automatica, acesse o VUUPT,
abra cada pedido e clique em <strong>Validar Canhoto</strong>.</p></div>
<p style="font-size:12px;color:#6B7280;margin-top:20px;">
Freshlog Logistica -- notificacao automatica do agente de expedicao.</p>
</div></body></html>"""

    assunto = f"[Freshlog] {len(pendentes)} canhoto(s) aguardando validacao no VUUPT".replace("\r", " ").replace("\n", " ")
    destino = "hugo@freshlogbr.com" if modo_teste else responsavel

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = remetente
        msg["To"]      = destino
        msg.attach(MIMEText(corpo, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(remetente, senha)
            smtp.send_message(msg)
        logger.info(f"  Notificacao de validacao pendente enviada para {destino}")
    except Exception as e:
        logger.warning(f"  Falha ao enviar notificacao: {e}")
        for s in pendentes:
            logger.warning(f"  Pendente: {s.get('code')}")


_PADRAO_CODIGO_BASE = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)


def duplicar_servico_por_insucesso(vuupt, servico_original: dict) -> dict | None:
    """
    Cria um NOVO serviço a partir de um insucesso de entrega, pra nova
    tentativa -- pedido do Hugo, 03/08: "duplicar dependendo o motivo
    do insucesso". Usa o campo NATIVO do VUUPT `recreated_order_
    origin_id` (confirmado em dado real de produção) pra rastrear a
    origem -- assim dá pra ver na tela do VUUPT que esse pedido veio
    de uma recriação, e de qual serviço original.

    Copia os dados essenciais do original (endereço, contato,
    remetente, coordenadas) -- o novo nasce como not_assigned, sem
    scheduled_start, e entra no próximo ciclo de roteirização
    normalmente. O `code` ganha um sufixo incremental (-R1, -R2...)
    pra não colidir com o original nem com duplicações anteriores.

    Parte do código BASE do original, não do code literal (achado
    20/08: uma entrega que já era reentrega, ao falhar de novo, gerava
    'PS-36741-R1' -> 'PS-36741-R1-R1' em vez de 'PS-36741-R2' -- o
    sufixo empilhava a cada nova tentativa, e a normalização de
    "código base" rio abaixo (romaneio, expedição) não dava conta de
    desempilhar uma cadeia arbitrária de sufixos). Extrair o prefixo
    'PS-NNNNN' aqui garante que mesmo a 3ª, 4ª... tentativa do mesmo
    pedido sempre gera um código limpo, de um sufixo só.

    Retorna o JSON do serviço criado, ou None se a criação falhar
    (loga o erro, não propaga exceção -- quem chama decide como
    tratar uma falha de duplicação).
    """
    code_bruto = (servico_original.get("code") or "").lstrip("#")
    m = _PADRAO_CODIGO_BASE.match(code_bruto)
    code_original = m.group(0) if m else code_bruto
    sufixo = 1
    novo_code = f"#{code_original}-R{sufixo}"
    while vuupt.buscar_servico_por_code(novo_code):
        sufixo += 1
        novo_code = f"#{code_original}-R{sufixo}"

    payload = {
        "code": novo_code,
        "title": f"{servico_original.get('title', '')} (reentrega R{sufixo})",
        "type": servico_original.get("type", "delivery"),
        "address": servico_original.get("address"),
        "address_complement": servico_original.get("address_complement"),
        "phone_number": servico_original.get("phone_number"),
        "latitude": servico_original.get("latitude"),
        "longitude": servico_original.get("longitude"),
        "sender_id": servico_original.get("sender_id"),
        "customer_id": servico_original.get("customer_id"),
        "duration_id": servico_original.get("duration_id"),
        "zone_id": servico_original.get("zone_id"),
        "recreated_order_origin_id": servico_original.get("id"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        novo_servico = vuupt.criar_servico(payload)
        logger.info(f"  Serviço duplicado: {servico_original.get('code')} -> {novo_code} "
                   f"(motivo do insucesso: {texto_do_motivo(servico_original.get('failed_reason_id'))})")
        # Garante o code no retorno: a resposta de criação do VUUPT não
        # ecoa 'code' na raiz (achado 12/08 na torre: fingerprints de
        # duplicação todos com novo_code vazio) -- o code correto é o
        # que acabou de ir no payload.
        return {**(novo_servico or {}), "code": novo_code}
    except Exception as e:
        logger.error(f"  Falha ao duplicar {servico_original.get('code')}: {e}")
        return None


def notificar_insucesso_entrega(insucessos: list, config_email: dict, modo_teste: bool):
    """
    Envia e-mail interno listando pedidos com INSUCESSO na entrega
    (status_done='failed' no VUUPT). Esses pedidos NÃO são expedidos na
    Stokki — só geram este aviso, pra alguém tratar manualmente
    (decisão do Hugo, 29/07). Mesmo padrão visual/estrutural de
    notificar_validacao_pendente, adaptado pro conteúdo de insucesso.
    """
    if not insucessos:
        return

    # A busca usa janela de HORAS_PADRAO pra duplicação/agendamento não
    # perder nada entre execuções, mas isso fazia este e-mail repetir
    # insucessos de ontem a cada 30 min (pedido do Hugo, 12/08 e 13/08:
    # e-mail só com os insucessos DO DIA, sem repetição). O filtro vale só
    # pra notificação -- a janela de HORAS_PADRAO segue intacta pra
    # duplicação, que tem fingerprint próprio. Entram no e-mail os insucessos de hoje
    # + qualquer um nunca notificado (ex.: concluído ontem depois da
    # última execução de 19h30, que senão sumiria sem aviso), e o e-mail
    # só sai se houver algum NOVO desde o último envio.
    hoje_local = datetime.now(FUSO_LOCAL).date()
    de_hoje = [
        s for s in insucessos
        if (dt := _parse_data(s.get("completed_at"))) and dt.astimezone(FUSO_LOCAL).date() == hoje_local
    ]
    novos = [s for s in insucessos if not fingerprint_notificacao_interna.ja_notificado(s.get("id"))]
    if not novos:
        logger.info("  Nenhum insucesso novo desde o ultimo e-mail -- notificacao interna pulada.")
        return
    por_id = {s.get("id"): s for s in de_hoje}
    for s in novos:
        por_id.setdefault(s.get("id"), s)
    insucessos = sorted(por_id.values(), key=lambda s: s.get("completed_at") or "")

    remetente   = config_email.get("remetente", "")
    senha       = config_email.get("senha_app") or config_email.get("senha", "")
    responsavel = config_email.get("email_responsavel", "")

    if not responsavel:
        logger.info("E-mail de responsavel nao configurado -- notificacao de insucesso pulada.")
        for s in insucessos:
            logger.warning(f"  Insucesso na entrega: {s.get('code')}")
        return

    COR_HEADER   = "#141428"
    COR_DESTAQUE = "#F5A623"
    COR_ERRO     = "#EF4444"

    linhas = ""
    for s in insucessos:
        completed = (s.get("completed_at") or "")[:16]
        motivo = s.get("note") or texto_do_motivo(s.get("failed_reason_id"))
        linhas += (
            f"<tr>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-weight:600;"
            f"font-size:13px;color:#1F2937;'>{html.escape(s.get('code','') or '')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{html.escape(s.get('title','') or '')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{html.escape(completed)}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:{COR_ERRO};'>{html.escape(motivo or '')}</td>"
            f"</tr>"
        )

    corpo = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;">
<div style="background:{COR_ERRO};padding:20px;border-radius:8px 8px 0 0;">
<h2 style="color:#fff;margin:0;">Pedidos com Insucesso na Entrega</h2></div>
<div style="background:#f9f9f9;padding:24px;border:1px solid #E5E7EB;border-top:none;border-radius:0 0 8px 8px;">
<p style="color:#1F2937;">Os pedidos abaixo foram marcados no VUUPT como <strong>entrega com insucesso</strong>
(status_done=failed). <strong>Eles NÃO foram expedidos na Stokki</strong> — precisam de tratamento manual
(redespacho, contato com o cliente, cancelamento, conforme o caso).</p>
<table style="width:100%;border-collapse:collapse;margin:16px 0;">
<thead><tr style="background:{COR_HEADER};">
<th style="padding:10px 14px;text-align:left;color:#fff;font-size:12px;">Pedido</th>
<th style="padding:10px 14px;text-align:left;color:#fff;font-size:12px;">Título</th>
<th style="padding:10px 14px;text-align:left;color:#fff;font-size:12px;">Concluído em</th>
<th style="padding:10px 14px;text-align:left;color:#fff;font-size:12px;">Motivo</th>
</tr></thead><tbody>{linhas}</tbody></table>
<div style="background:#FFF8EC;border-left:4px solid {COR_DESTAQUE};border-radius:6px;padding:14px 18px;">
<p style="margin:0;font-size:13px;color:#1F2937;">Acesse o VUUPT pra ver o detalhe completo de cada
insucesso e decidir o próximo passo (nova tentativa, redespacho, cancelamento etc.).</p></div>
<p style="font-size:12px;color:#6B7280;margin-top:20px;">
Freshlog Logistica -- notificacao automatica do agente de expedicao.</p>
</div></body></html>"""

    assunto = f"[Freshlog] {len(insucessos)} pedido(s) com insucesso na entrega".replace("\r", " ").replace("\n", " ")
    destino = "hugo@freshlogbr.com" if modo_teste else responsavel

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = remetente
        msg["To"]      = destino
        msg.attach(MIMEText(corpo, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(remetente, senha)
            smtp.send_message(msg)
        logger.info(f"  Notificacao de insucesso de entrega enviada para {destino}")
        # Marca só depois do envio dar certo (falha de SMTP tenta de novo
        # na próxima execução) e nunca em modo_teste (teste não pode
        # suprimir o e-mail de produção seguinte).
        if not modo_teste:
            fingerprint_notificacao_interna.marcar_notificados(insucessos)
    except Exception as e:
        logger.warning(f"  Falha ao enviar notificacao de insucesso: {e}")
        for s in insucessos:
            logger.warning(f"  Insucesso: {s.get('code')}")


def notificar_sem_insucesso_hoje(config_email: dict, modo_teste: bool):
    """
    E-mail interno de "dia limpo" (pedido do Hugo, 13/08): quando o dia
    termina sem NENHUM insucesso de entrega, avisa isso explicitamente
    -- sem ele, a ausência do e-mail de insucesso seria ambígua (dia
    limpo ou agente quebrado?). Só sai nas execuções finais do dia
    (>= HORA_EMAIL_SEM_INSUCESSO; a tarefa roda até 19h30) e no máximo
    1 vez por dia (fingerprint).
    """
    agora = datetime.now(FUSO_LOCAL)
    if agora.hour < HORA_EMAIL_SEM_INSUCESSO:
        return
    data_iso = agora.date().isoformat()
    if fingerprint_notificacao_interna.sem_insucesso_ja_enviado(data_iso):
        return

    remetente   = config_email.get("remetente", "")
    senha       = config_email.get("senha_app") or config_email.get("senha", "")
    responsavel = config_email.get("email_responsavel", "")
    if not responsavel:
        logger.info("E-mail de responsavel nao configurado -- aviso de dia sem insucesso pulado.")
        return

    COR_OK   = "#00C896"
    data_fmt = agora.strftime("%d/%m/%Y")

    corpo = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
<div style="background:{COR_OK};padding:20px;border-radius:8px 8px 0 0;">
<h2 style="color:#fff;margin:0;">Sem Insucessos na Entrega Hoje</h2></div>
<div style="background:#f9f9f9;padding:24px;border:1px solid #E5E7EB;border-top:none;border-radius:0 0 8px 8px;">
<p style="color:#1F2937;">Nenhum pedido foi marcado como <strong>entrega com insucesso</strong> no VUUPT
hoje ({data_fmt}). Nenhuma ação necessária.</p>
<p style="font-size:12px;color:#6B7280;margin-top:20px;">
Freshlog Logistica -- notificacao automatica do agente de expedicao.</p>
</div></body></html>"""

    assunto = f"[Freshlog] Sem insucessos na entrega hoje ({data_fmt})"
    destino = "hugo@freshlogbr.com" if modo_teste else responsavel

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = assunto
        msg["From"]    = remetente
        msg["To"]      = destino
        msg.attach(MIMEText(corpo, "html", "utf-8"))

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(remetente, senha)
            smtp.send_message(msg)
        logger.info(f"  Aviso de dia sem insucesso enviado para {destino}")
        if not modo_teste:
            fingerprint_notificacao_interna.marcar_sem_insucesso_enviado(data_iso)
    except Exception as e:
        logger.warning(f"  Falha ao enviar aviso de dia sem insucesso: {e}")


# ── Stokki ────────────────────────────────────────────────────────────────────

def _extrair_id(codigo_ps: str) -> str:
    # Primeira sequência de dígitos, NÃO a última: reentrega tem código
    # 'PS-36327-R1' e r'(\d+)$' extraía o '1' do sufixo (id errado).
    m = re.search(r"(\d+)", codigo_ps)
    return m.group(1) if m else ""


def expedir_na_stokki(page, codigo_ps: str) -> bool:
    """
    Expede o pedido usando fetch() nativo do browser via page.evaluate(),
    que compartilha corretamente os cookies da sessao do /provider/.
    """
    id_stokki = _extrair_id(codigo_ps)
    if not id_stokki:
        return False

    # Garante que estamos na area /provider/ para ter os cookies corretos
    if "/provider/" not in page.url:
        page.goto(
            f"{STOKKI_BASE}/pt-br/provider/operation/shipping",
            wait_until="domcontentloaded", timeout=30_000,
        )
        page.wait_for_timeout(1000)

    # Tenta CSRF da meta tag; se nao tiver, usa o cookie XSRF-TOKEN (padrao Laravel)
    csrf = page.evaluate("""() => {
        const meta = document.querySelector('meta[name=csrf-token]');
        if (meta && meta.content) return meta.content;
        const cookie = document.cookie.split('; ')
            .find(c => c.startsWith('XSRF-TOKEN='));
        if (cookie) return decodeURIComponent(cookie.split('=').slice(1).join('='));
        return '';
    }""")

    hora_atual = datetime.now().strftime("%H:%M")

    resultado = page.evaluate(
        """async ([url, id, csrf, hora]) => {
            try {
                const fd = new FormData();
                fd.append('provider_outbound_id[]', id);
                fd.append('hour', hora);
                fd.append('print_waybill', 'no');
                fd.append('carrier_id', '');
                fd.append('_token', csrf);
                const r = await fetch(url, {
                    method: 'POST',
                    body: fd,
                    headers: {'X-Requested-With': 'XMLHttpRequest'}
                });
                const texto = await r.text();
                return {status: r.status, body: texto};
            } catch(e) {
                return {status: 0, body: String(e)};
            }
        }""",
        [URL_SHIPPING, id_stokki, csrf, hora_atual],
    )

    status = resultado.get("status", 0)
    corpo  = resultado.get("body", "")
    logger.info(f"  Expedicao {codigo_ps}: status={status} resposta={corpo[:200]}")

    if status == 200 and ('"success":true' in corpo or '"success": true' in corpo):
        logger.info(f"  Expedido com sucesso: {codigo_ps}")
        return "expedido"

    if status == 404 and "situa" in corpo.lower() and "inv" in corpo.lower():
        logger.info(f"  {codigo_ps} ja estava expedido -- pulando expedicao, tentara anexar.")
        return "ja_expedido"

    logger.warning(f"  Falha na expedicao de {codigo_ps}: status={status}")
    return "falha"


def _credenciais_provider(config: dict) -> tuple[str, str]:
    """Retorna (usuario, senha) para login na area /provider/ do Stokki."""
    provider = config.get("stokki", {}).get("provider", {})
    usuario = provider.get("usuario") or config.get("stokki", {}).get("usuario", "")
    senha   = provider.get("senha")   or config.get("stokki", {}).get("senha", "")
    return usuario, senha


def _novo_contexto_disfarcado(browser):
    """Contexto do Chromium com UA de navegador real e navigator.webdriver
    mascarado. Sem isso a Stokki bloqueia com 403 ("Acesso automatizado
    nao e permitido") -- o Chromium headless por padrao expoe
    "HeadlessChrome" no User-Agent e navigator.webdriver=true (mesmo
    problema e fix de stokki/auth.py, stokki/estacao_impressao.py e
    documentos_pedido/stokki_documentos.py, 20/08)."""
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"
    )
    context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return context


def _preencher_form_login(page, usuario: str, senha: str, tentativas: int = 3, timeout_ms: int = 15_000):
    """Preenche e envia o formulário de login do /provider/, com
    retentativas -- mesmo helper de stokki/estacao_impressao.py, 20/08:
    o Stokki às vezes demora mais que o timeout pra renderizar o
    formulário (lento/instável). Cada tentativa recarrega a página antes
    de tentar de novo."""
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            page.wait_for_selector("[name='email']", timeout=timeout_ms)
            page.fill("[name='email']", usuario)
            page.fill("[name='password']", senha)
            page.click("button[type='submit']")
            page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
            return
        except Exception as e:
            ultimo_erro = e
            logger.warning(f"  Falha no formulario de login (tentativa {tentativa}/{tentativas}): {e}")
            if tentativa < tentativas:
                try:
                    page.reload(timeout=timeout_ms)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
    raise ultimo_erro


def _setup_playwright(config: dict):
    from playwright.sync_api import sync_playwright
    usuario, senha = _credenciais_provider(config)
    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    try:
        ctx  = _novo_contexto_disfarcado(browser)
        page = ctx.new_page()

        # Login direto pelo /pt-br/login (pagina de login do provider)
        logger.info(f"Login provider com usuario: {usuario!r}")
        page.goto(f"{STOKKI_BASE}/pt-br/login", wait_until="domcontentloaded", timeout=30_000)
        _preencher_form_login(page, usuario, senha)
        page.wait_for_timeout(1000)
        logger.info(f"Login OK. URL: {page.url}")

        # Navega para a estacao de expedicao
        page.goto(
            f"{STOKKI_BASE}/pt-br/provider/operation/shipping",
            wait_until="domcontentloaded",
            timeout=20_000,
        )
        page.wait_for_timeout(1000)
        logger.info(f"Estacao de expedicao. URL: {page.url}")
    except Exception:
        # Se o login falhar (seletor mudou, timeout, credencial invalida),
        # fecha o que ja foi aberto antes de propagar -- sem isso, o
        # browser/processo Playwright ficava orfao, ja que o try/finally
        # de quem chama so comeca a proteger DEPOIS que esta funcao retorna.
        browser.close()
        pw.stop()
        raise

    return pw, browser, page


def _canhoto_ja_anexado(page, codigo_ps: str) -> bool:
    """
    Verifica se ja existe um 'Comprovante de Entrega' na aba Documentos
    do pedido. Retorna True se ja foi anexado anteriormente.
    """
    try:
        page.goto(
            f"{URL_PROVIDER_SHW}/{_extrair_id(codigo_ps)}",
            wait_until="networkidle",
            timeout=30_000,
        )
        page.wait_for_timeout(1000)

        # Clica na aba Documentos
        page.click("#document-tab", timeout=8_000)
        page.wait_for_timeout(800)

        # Procura por "Comprovante de Entrega" na aba
        conteudo = page.inner_text("#document", timeout=5_000)
        if "comprovante de entrega" in conteudo.lower():
            logger.info(f"  {codigo_ps}: canhoto ja anexado anteriormente -- pulando.")
            return True
        return False
    except Exception as e:
        logger.debug(f"  Verificacao de canhoto existente falhou: {e}")
        return False  # em caso de duvida, tenta anexar


def anexar_canhoto(page, codigo_ps: str, pdf_path: Path) -> bool:
    # Esperas fixas reduzidas (04/08, pedido do Hugo: processos demorando demais).
    # page.click()/page.fill() do Playwright já esperam sozinhos o elemento
    # ficar clicável/preenchível (até o timeout de cada chamada) -- a maior
    # parte dessas pausas fixas era margem extra POR CIMA disso, herdada de
    # quando esse fluxo foi escrito. Reduzidas, não removidas -- mantém uma
    # folga pequena pros casos em que a UI realmente demora a atualizar.
    # Se algo começar a falhar depois dessa mudança, é sinal de que aquele
    # valor específico precisa voltar a subir (não os outros).
    try:
        page.goto(
            f"{URL_PROVIDER_SHW}/{_extrair_id(codigo_ps)}",
            wait_until="networkidle",
            timeout=30_000,
        )
        page.wait_for_timeout(400)  # era 1500

        seletores_tab = ["#document-tab", "a[href=\'#document \']", "a:text(\'Documentos\')"]
        clicou_tab = False
        for sel in seletores_tab:
            try:
                page.click(sel, timeout=5_000)
                clicou_tab = True
                break
            except Exception:
                continue

        if not clicou_tab:
            logger.warning(f"  Nao encontrou aba Documentos em {codigo_ps}")
            return False

        page.wait_for_timeout(250)  # era 800

        seletores_btn = ["a.btn_document.button-stokki", "a.dropdown-item.link-document-tab"]
        clicou_btn = False
        for sel in seletores_btn:
            try:
                page.click(sel, timeout=5_000)
                clicou_btn = True
                break
            except Exception:
                continue

        if not clicou_btn:
            logger.warning(f"  Botao de anexo nao encontrado em {codigo_ps}")
            return False

        page.wait_for_timeout(200)  # era 500
        page.fill("#type_document", "Comprovante de Entrega")

        with page.expect_file_chooser() as fc:
            page.click("#btn_file", timeout=10_000)
        fc.value.set_files(str(pdf_path))
        page.wait_for_timeout(500)  # era 1200 -- mantém mais folga aqui, upload de arquivo é menos previsível

        page.click("#btn_document_submit", timeout=15_000)
        page.wait_for_timeout(300)  # era 800

        logger.info(f"  Canhoto anexado: {codigo_ps}")
        return True
    except Exception as e:
        logger.warning(f"  Falha ao anexar {codigo_ps}: {e}")
        return False


# ── Orquestrador ───────────────────────────────────────────────────────────────

def main(horas: int = HORAS_PADRAO, modo_teste: bool = False, limite: int = 0,
         forcar: list = None):
    prefixo = "[MODO TESTE] " if modo_teste else ""
    logger.info(f"{prefixo}Expedicao iniciada (janela: {horas}h)")

    config       = _carregar_config()
    vuupt_token  = config.get("vuupt_api", {}).get("token", "")
    config_email = config.get("email", {})
    if not vuupt_token:
        raise SystemExit("Token VUUPT nao configurado em config.yaml")

    # Modo forcado: expede lista de codigos PS sem verificar VUUPT
    if forcar:
        codigos = [c.strip().lstrip("#") for c in forcar]
        logger.info(f"MODO FORCADO: {len(codigos)} pedido(s): {codigos}")
        validados = [{"code": c, "checklistAnswers": {"data": []}} for c in codigos]
        pendentes = []
    else:
        # 1. Busca entregues com canhoto
        servicos = buscar_servicos_entregues(vuupt_token, horas=horas)
        if not servicos:
            # Sem return: mesmo sem canhoto novo, a parte de INSUCESSO
            # abaixo precisa rodar (notificação, duplicação, aviso de dia
            # sem insucesso) -- o gate da expedição em si é o
            # "if not validados" mais adiante.
            logger.info("Nenhum pedido com canhoto no periodo.")

        # 2. Separa validados dos pendentes
        validados = [s for s in servicos if canhoto_validado(s)]
        pendentes = [s for s in servicos if not canhoto_validado(s)]
        logger.info(f"Validados no VUUPT: {len(validados)} | Pendentes de validacao: {len(pendentes)}")

        # 2b. Filtra quem já foi expedido + canhoto tratado numa execução
        # anterior -- nem entra na lista, evita reprocessar (pedido do
        # Hugo, 30/07: "logo de início nem entraria mais na lista")
        antes = len(validados)
        validados = [s for s in validados if not fingerprint_expedicao.ja_processado(s.get("code"))]
        pulados = antes - len(validados)
        if pulados:
            logger.info(f"{pulados} pedido(s) já expedido(s)/tratado(s) antes -- pulando (fingerprint).")

        # 3. Notifica pendentes
        if pendentes:
            codigos_pend = [s.get('code') for s in pendentes]
            logger.info(f"Pendentes (nao serao expedidos): {codigos_pend}")
            notificar_validacao_pendente(pendentes, config_email, modo_teste)

        # 3b. Busca insucessos de entrega (status_done='failed'). NÃO
        # exige canhoto/checklist validado -- confirmado com dado real
        # (03/08): insucessos nunca têm checklistAnswers preenchido,
        # então esse gate deixava a notificação (e a duplicação) presas
        # pra sempre. NÃO expedidos na Stokki.
        #
        # REGRA ATUAL (pedido do Hugo, 15/08): NENHUM insucesso duplica
        # sozinho. O remetente recebe uma PERGUNTA ("houve insucesso,
        # deseja o reenvio?") e só duplicamos se a resposta confirmar --
        # a resposta é dada numa página pública (18/08, ver
        # insucesso_entrega/notificar_insucesso_aguardando_resposta.py e
        # resposta_insucesso/app.py, hospedada na mesma VPS em
        # app.freshhub.com.br/insucesso) que já aplica a decisão no VUUPT
        # NA HORA do clique -- não existe mais um passo de sincronização
        # aqui (a página lê/escreve direto no mesmo dados.db). (Revoga a
        # regra de 11/08 -- duplicava tudo na hora e só perguntava
        # depois -- que por sua vez tinha revogado a regra original de
        # 03/08 de perguntar antes.)

        insucessos = buscar_servicos_insucesso(vuupt_token, horas=horas)
        if insucessos:
            codigos_insucesso = [s.get('code') for s in insucessos]
            logger.info(f"Insucesso na entrega (nao serao expedidos): {codigos_insucesso}")

            for s in insucessos:
                motivo_texto_s = texto_do_motivo(s.get("failed_reason_id"))
                if s.get("code") and not tratativas.ja_registrado(s["code"], "INSUCESSO_DETECTADO"):
                    tratativas.registrar_evento(
                        s["code"], "INSUCESSO_ENTREGA", "INSUCESSO_DETECTADO",
                        service_id=s.get("id"), motivo_id=s.get("failed_reason_id"),
                        motivo_texto=motivo_texto_s,
                    )

            # SEM duplicação automática (pedido do Hugo, 15/08: "quero
            # que as tratativas definam se vamos ou não duplicar um
            # pedido, não quero mais duplicar automaticamente" -- revoga
            # a regra de 11/08 abaixo). A ÚNICA coisa que cria uma
            # reentrega agora é a resposta do embarcador ao e-mail de
            # "aguardando retorno" (aplicar_resposta_insucesso.py::
            # _cancelar_reentrega/_reagendar_reentrega/branch "manter" --
            # essas três funções já sabem duplicar na hora quando ainda
            # não existe nada duplicado/agendado pra esse pedido, era o
            # caminho de transição do fluxo antigo e virou o caminho
            # principal). Se o embarcador não responder, nada acontece:
            # "no caso do cliente responder, prevalece o que ele
            # solicitar" -- sem resposta, sem ação.
            pendentes_resposta = identificar_aguardando_resposta(insucessos)
            if pendentes_resposta:
                resultado_espera = notificar_insucesso_aguardando_resposta(
                    pendentes_resposta, config_email, config.get("resposta_insucesso", {}), modo_teste
                )
                logger.info(f"Pergunta de reenvio aos remetentes: {resultado_espera}")

            notificar_insucesso_entrega(insucessos, config_email, modo_teste)

        # Dia sem NENHUM insucesso: no fim do dia avisa explicitamente
        # (pedido do Hugo, 13/08) -- a função decide sozinha o horário
        # (>= 19h) e o limite de 1 e-mail por dia.
        hoje_local = datetime.now(FUSO_LOCAL).date()
        teve_insucesso_hoje = any(
            (dt := _parse_data(s.get("completed_at"))) and dt.astimezone(FUSO_LOCAL).date() == hoje_local
            for s in insucessos
        )
        if not teve_insucesso_hoje:
            notificar_sem_insucesso_hoje(config_email, modo_teste)

        # Drena duplicações agendadas ANTES de 15/08 (regra revogada --
        # nada novo entra aqui, ver comentário acima; isto só escoa o
        # que já estava agendado quando a regra mudou, até esvaziar).
        vencidas = fingerprint_duplicacao_agendada.buscar_pendentes_vencidas()
        if vencidas:
            logger.info(f"{len(vencidas)} duplicação(ões) agendada(s) vencida(s) -- processando.")
            vuupt_para_agendadas = VuuptClient(vuupt_token)
            for pendente in vencidas:
                if modo_teste:
                    logger.info(f"  [TESTE] Duplicaria (agendada) {pendente['code']} "
                               f"(vencida em {pendente['data_agendada']}).")
                    continue
                servico_original = vuupt_para_agendadas.buscar_servico_por_code(pendente["code"])
                if not servico_original:
                    logger.warning(f"  {pendente['code']}: não encontrado mais no VUUPT -- "
                                   f"marcando como falha, não tenta de novo.")
                    fingerprint_duplicacao_agendada.marcar_falha(pendente["service_id"], "Serviço não encontrado no VUUPT")
                    continue
                novo = duplicar_servico_por_insucesso(vuupt_para_agendadas, servico_original)
                if novo:
                    fingerprint_duplicacao_agendada.marcar_executada(pendente["service_id"], novo.get("code", ""))
                    fingerprint_duplicacao_insucesso.marcar_duplicado(pendente["service_id"], novo.get("code", ""))
                    logger.info(f"  Duplicação agendada executada: {pendente['code']} -> {novo.get('code')}")
                    tratativas.registrar_evento(
                        pendente["code"], "INSUCESSO_ENTREGA", "REENVIO_AUTOMATICO",
                        service_id=pendente.get("service_id"), motivo_id=pendente.get("failed_reason_id"),
                        motivo_texto=texto_do_motivo(pendente.get("failed_reason_id")),
                        texto=f"Reentrega agendada executada (novo código: {novo.get('code', '')}).",
                    )
                else:
                    fingerprint_duplicacao_agendada.marcar_falha(pendente["service_id"], "Falha ao criar o novo serviço")

        if not validados:
            logger.info("Nenhum pedido com canhoto validado. Nada a expedir.")
            return

    if limite > 0:
        validados = validados[:limite]
        logger.info(f"Limite: processando {len(validados)} pedido(s).")

    if modo_teste:
        logger.info("[MODO TESTE] Seriam expedidos:")
        for s in validados:
            cl = _extrair_checklist(s)
            logger.info(
                f"  {s.get('code')} | vuupt_id={s.get('id')} | "
                f"checklist_id={cl.get('id') if cl else '?'} | "
                f"validado_em={(cl or {}).get('validated_at','')[:16]}"
            )
        return

    # 4. Setup Playwright provider (expedicao e anexo)
    pw, browser, page = _setup_playwright(config)

    res = {"expedido": 0, "falha": 0, "anexado": 0, "falha_anexo": 0, "sem_pdf": 0}

    total = len(validados)
    try:
        for i, servico in enumerate(validados, 1):
            codigo_ps    = servico.get("code", "")
            checklist_id = extrair_checklist_id(servico)
            cl           = _extrair_checklist(servico)
            logger.info(
                f"[{i}/{total}] {codigo_ps} | "
                f"validado_em={(cl or {}).get('validated_at','')[:16]}"
            )

            pdf_path = baixar_canhoto_pdf(vuupt_token, checklist_id, codigo_ps) if checklist_id else None

            resultado_exp = expedir_na_stokki(page, codigo_ps)

            if resultado_exp in ("expedido", "ja_expedido"):
                if resultado_exp == "expedido":
                    res["expedido"] += 1
                else:
                    res["ja_expedido"] = res.get("ja_expedido", 0) + 1
                if pdf_path:
                    if _canhoto_ja_anexado(page, codigo_ps):
                        res["anexado"] += 1  # conta como ok -- ja estava la
                        fingerprint_expedicao.marcar_processado(codigo_ps, servico.get("id"))
                    elif anexar_canhoto(page, codigo_ps, pdf_path):
                        res["anexado"] += 1
                        fingerprint_expedicao.marcar_processado(codigo_ps, servico.get("id"))
                    else:
                        res["falha_anexo"] += 1
                else:
                    res["sem_pdf"] += 1
                    logger.warning(f"  {codigo_ps}: sem canhoto para anexar.")
            else:
                res["falha"] += 1

            time.sleep(0.5)  # era 1.5s (04/08, mesmo motivo: margem generosa demais herdada)
    finally:
        browser.close()
        pw.stop()

    logger.info("=" * 60)
    logger.info(
        f"RESUMO: Expedidos={res['expedido']} | Ja expedidos={res.get('ja_expedido',0)} | "
        f"Falhas={res['falha']} | Canhotos anexados={res['anexado']} | "
        f"Falha anexo={res['falha_anexo']} | Sem PDF={res['sem_pdf']}"
    )
    if res.get("anexado") or res.get("falha_anexo"):
        logger.info(f"Canhotos (PDF) salvos em: {CANHOTOS_DIR.resolve()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--horas", type=int, default=HORAS_PADRAO)
    parser.add_argument("--limite", type=int, default=0)
    parser.add_argument("--modo-teste", action="store_true")
    parser.add_argument("--forcar", nargs="+", metavar="PS-XXXXX",
                        help="Expede forcadamente os codigos informados, sem verificar VUUPT")
    args = parser.parse_args()
    main(horas=args.horas, modo_teste=args.modo_teste, limite=args.limite,
         forcar=args.forcar)
