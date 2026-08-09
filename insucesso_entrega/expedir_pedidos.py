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
import logging
import re
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
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
from motivos_falha import texto_do_motivo, deve_duplicar, duplicar_com_atraso
from notificar_insucesso_aguardando_resposta import (
    identificar_aguardando_resposta, notificar_remetentes as notificar_insucesso_aguardando_resposta,
)
from vuupt_client import VuuptClient


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

HORAS_PADRAO = 48


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
                "include": "checklistAnswers",
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
            f"font-size:13px;color:#1F2937;'>{s.get('code','')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{filled}</td>"
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

    assunto = f"[Freshlog] {len(pendentes)} canhoto(s) aguardando validacao no VUUPT"
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


def texto_do_motivo_e_deve_duplicar(servico: dict) -> tuple[str, bool]:
    """Atalho: texto compreensível + se deve duplicar, a partir do
    failed_reason_id do serviço."""
    rid = servico.get("failed_reason_id")
    return texto_do_motivo(rid), deve_duplicar(rid)


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

    Retorna o JSON do serviço criado, ou None se a criação falhar
    (loga o erro, não propaga exceção -- quem chama decide como
    tratar uma falha de duplicação).
    """
    code_original = (servico_original.get("code") or "").lstrip("#")
    sufixo = 1
    novo_code = f"{code_original}-R{sufixo}"
    while vuupt.buscar_servico_por_code(novo_code):
        sufixo += 1
        novo_code = f"{code_original}-R{sufixo}"

    payload = {
        "code": novo_code,
        "title": f"{servico_original.get('title', '')} (reentrega)",
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
        return novo_servico
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
            f"font-size:13px;color:#1F2937;'>{s.get('code','')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{s.get('title','')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{completed}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:{COR_ERRO};'>{motivo}</td>"
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
<p style="margin:0;font-size:13px;color:#1F2937;">Acesse o VUUPT para ver o detalhe completo de cada
insucesso e decidir o próximo passo (nova tentativa, redespacho, cancelamento etc.).</p></div>
<p style="font-size:12px;color:#6B7280;margin-top:20px;">
Freshlog Logistica -- notificacao automatica do agente de expedicao.</p>
</div></body></html>"""

    assunto = f"[Freshlog] {len(insucessos)} pedido(s) com insucesso na entrega"
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
    except Exception as e:
        logger.warning(f"  Falha ao enviar notificacao de insucesso: {e}")
        for s in insucessos:
            logger.warning(f"  Insucesso: {s.get('code')}")


# ── Stokki ────────────────────────────────────────────────────────────────────

def _extrair_id(codigo_ps: str) -> str:
    m = re.search(r"(\d+)$", codigo_ps)
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


def _setup_playwright(config: dict):
    from playwright.sync_api import sync_playwright
    usuario, senha = _credenciais_provider(config)
    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context()
    page    = ctx.new_page()

    # Login direto pelo /pt-br/login (pagina de login do provider)
    logger.info(f"Login provider com usuario: {usuario!r}")
    page.goto(f"{STOKKI_BASE}/pt-br/login", wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("[name=\'email\']", timeout=15_000)
    page.fill("[name=\'email\']", usuario)
    page.fill("[name=\'password\']", senha)
    page.click("button[type=\'submit\']")
    page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
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
    """Sessao separada do administrador para anexar documentos."""
    from playwright.sync_api import sync_playwright
    usuario = config.get("stokki", {}).get("usuario", "")
    senha   = config.get("stokki", {}).get("senha", "")
    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context()
    page    = ctx.new_page()
    page.goto(URL_LOGIN, wait_until="domcontentloaded", timeout=30_000)
    page.fill("[name=\'email\']", usuario)
    page.fill("[name=\'password\']", senha)
    page.click("button[type=\'submit\']")
    page.wait_for_url(lambda u: "login" not in u, timeout=30_000)
    return pw, browser, page


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
            logger.info("Nenhum pedido com canhoto no periodo.")
            return

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
        # pra sempre. Decisão do Hugo, 03/08: notifica e decide a
        # duplicação direto pelo MOTIVO (failed_reason_id), sem esperar
        # validação nenhuma. NÃO expedidos na Stokki.
        insucessos = buscar_servicos_insucesso(vuupt_token, horas=horas)
        if insucessos:
            codigos_insucesso = [s.get('code') for s in insucessos]
            logger.info(f"Insucesso na entrega (nao serao expedidos): {codigos_insucesso}")

            for s in insucessos:
                if deve_duplicar(s.get("failed_reason_id")) and not fingerprint_duplicacao_insucesso.ja_duplicado(s.get("id")):
                    motivo_texto = texto_do_motivo(s.get("failed_reason_id"))
                    if modo_teste:
                        logger.info(f"  [TESTE] Duplicaria {s.get('code')} (motivo: {motivo_texto})")
                    else:
                        novo = duplicar_servico_por_insucesso(VuuptClient(vuupt_token), s)
                        if novo:
                            fingerprint_duplicacao_insucesso.marcar_duplicado(s.get("id"), novo.get("code", ""))

            # Pra alguns motivos (pedido do Hugo, 06/08), a duplicação
            # não é imediata nem espera resposta -- é agendada pra N
            # dias úteis depois da detecção (ex: Loja/Câmara em
            # Manutenção, dá tempo da manutenção terminar sozinha).
            for s in insucessos:
                dias_uteis = duplicar_com_atraso(s.get("failed_reason_id"))
                if dias_uteis and not fingerprint_duplicacao_agendada.ja_agendado(s.get("id")):
                    if modo_teste:
                        logger.info(f"  [TESTE] Agendaria duplicação de {s.get('code')} pra "
                                   f"{dias_uteis} dia(s) útil(eis) a partir de hoje.")
                    else:
                        data_agendada = fingerprint_duplicacao_agendada.agendar(
                            s.get("id"), s.get("code", ""), s.get("failed_reason_id"), dias_uteis,
                        )
                        logger.info(f"  Duplicação de {s.get('code')} agendada pra {data_agendada} "
                                   f"({dias_uteis} dia(s) útil(eis)).")

            # Pra alguns motivos (pedido do Hugo, 03/08), em vez de
            # duplicar, notifica o remetente com uma pergunta
            # específica e aguarda resposta antes de qualquer ação.
            pendentes_resposta = identificar_aguardando_resposta(insucessos)
            if pendentes_resposta:
                resultado_espera = notificar_insucesso_aguardando_resposta(
                    pendentes_resposta, config_email, modo_teste
                )
                logger.info(f"Notificação de insucesso aguardando resposta: {resultado_espera}")

            notificar_insucesso_entrega(insucessos, config_email, modo_teste)

        # Duplicações agendadas que já venceram (rodapé independente de
        # ter insucesso NOVO hoje -- uma agendada há alguns dias pode
        # vencer numa execução em que não apareceu nenhum insucesso novo).
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
