# -*- coding: utf-8 -*-
"""
validar_checklists.py

Agente de validação dos checklists (canhotos) dos motoristas -- pedido
do Hugo, 13/08. Substitui a validação manual no VUUPT:

  1. Busca no VUUPT os serviços ENTREGUES COM SUCESSO cujo checklist
     foi preenchido mas ainda não foi validado (validated_at=null).
  2. Baixa o PDF do canhoto (GET /checklists/{id}/print) e manda pra
     visão do Claude julgar contra o padrão de foto.
  3. APROVADA  -> valida via API (PUT /checklists/{id}/validate); o
     expedir_pedidos.py expede na Stokki no ciclo seguinte, sozinho.
  4. REPROVADA -> CLONA o pedido no VUUPT (recoleta de canhoto, código
     com sufixo -C1) e avisa por e-mail. O original fica pendente de
     validação manual (decisão do Hugo, 13/08).
  5. DÚVIDA    -> não valida nem clona; entra na seção "revisão manual"
     do e-mail e segue o fluxo manual de validação no VUUPT.

Critérios de REPROVAÇÃO (Hugo, 13/08) -- qualquer um reprova:
  - Ilegível ou borrada (não dá pra ler o conteúdo do documento)
  - Sem assinatura/identificação de quem recebeu
  - Documento errado (foto que não mostra o canhoto/NF: caixa, fachada,
    tela de celular...)
  - Cortada ou incompleta (parte do canhoto fora do enquadramento)
  - Checklist preenchido sem NENHUMA foto (caso extremo do mesmo padrão)

Execute:
  py -3.11 validar_checklists.py
  py -3.11 validar_checklists.py --horas 48 --limite 5
  py -3.11 validar_checklists.py --modo-teste
"""
import argparse
import base64
import hashlib
import html
import io
import json
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

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

import fingerprint_validacao
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
        logging.FileHandler(_RAIZ / "dados" / "validacao_checklists.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("validacao_checklists")
# O PDF do /print do VUUPT tem chave /PageMode duplicada -- warning
# inofensivo do pypdf a cada extração; silencia pra não poluir o log.
logging.getLogger("pypdf").setLevel(logging.ERROR)

CONFIG_PATH  = _RAIZ / "config.yaml"
CANHOTOS_DIR = _RAIZ / "dados" / "canhotos"  # mesmo cache do expedir_pedidos.py

VUUPT_BASE  = "https://api.vuupt.com/api/v1/services"
VUUPT_CHECK = "https://api.vuupt.com/api/v1/checklists"

ANTHROPIC_URL   = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-opus-5"

HORAS_PADRAO = 48

# Embarcadores que entregam SEM Nota Fiscal (mesmos sender_ids do VUUPT
# usados na canhoteira do gerar_pdf_romaneios.py): a prova de entrega
# deles é a folha de CANHOTEIRA do romaneio assinada, não o canhoto da
# NF -- sem esta exceção o critério "documento errado" reprovaria toda
# entrega deles (visto no primeiro caso real, PS-36507/Padrão Puro).
SENDERS_SEM_NF = {
    12887364,  # PADRÃO PURO
    21785428,  # QUATRO ESTRELAS
    21911340,  # PEDRAMOURA
}

# Saída estruturada garante JSON válido no formato exato -- sem parse
# defensivo de texto livre.
_SCHEMA_DECISAO = {
    "type": "object",
    "properties": {
        "decisao": {"type": "string", "enum": ["aprovada", "reprovada", "duvida"]},
        "motivo": {"type": "string"},
    },
    "required": ["decisao", "motivo"],
    "additionalProperties": False,
}

_PROMPT_VALIDACAO = """Você é o validador de canhotos de entrega da FreshLog (operadora logística).

O PDF anexo é o registro do checklist que o MOTORISTA preencheu ao concluir uma entrega.
Ele contém a(s) foto(s) tirada(s) pelo motorista, que deveriam mostrar o CANHOTO da Nota
Fiscal (ou documento de entrega equivalente) assinado por quem recebeu a mercadoria.
O PDF pode conter também cabeçalhos, dados de GPS, mapas e metadados do sistema -- ignore
isso; julgue APENAS a(s) foto(s). As imagens anexadas junto do PDF são as MESMAS fotos do
checklist em alta resolução -- use-as para julgar a legibilidade (a versão dentro do PDF
é reduzida); ignore imagens que sejam mapas ou logotipos do sistema.

IMPORTANTE sobre o formato: o canhoto é o destaque (tira destacável) do DANFE da Nota
Fiscal -- uma TIRA ESTREITA de papel. O formato estreito é normal e NÃO é motivo de
reprovação; o que importa é o conteúdo dessa tira estar legível, assinado e inteiro.

Julgue se a foto está DENTRO DO PADRÃO. A foto é REPROVADA se cair em QUALQUER um destes casos:

1. ILEGÍVEL OU BORRADA: não dá para ler o conteúdo do documento na foto (desfocada,
   escura, com reflexo que impede a leitura).
2. SEM ASSINATURA/RECEBEDOR: o canhoto aparece, mas sem assinatura, nome ou documento
   de quem recebeu.
3. DOCUMENTO ERRADO: a foto não mostra o canhoto/NF -- por exemplo foto da caixa, da
   fachada, do caminhão, de uma tela de celular, ou de qualquer outra coisa.
4. CORTADA OU INCOMPLETA: parte relevante do canhoto ficou fora do enquadramento
   (assinatura ou identificação cortada, documento pela metade).

A foto é APROVADA somente se claramente mostra o canhoto/documento de entrega, legível,
com assinatura ou identificação de quem recebeu, e com o documento inteiro no quadro.

Se for um caso limítrofe -- você não tem confiança para aprovar nem para reprovar
(ex.: parcialmente legível, assinatura possível mas incerta) -- responda "duvida".
Na dúvida, NUNCA aprove nem reprove: deixe para revisão humana.

Responda com a decisão e um motivo curto (1 frase, em português) explicando o porquê."""

_CONTEXTO_SEM_NF = """

ATENÇÃO -- EXCEÇÃO PARA ESTA ENTREGA: este embarcador entrega SEM Nota Fiscal.
A prova de entrega esperada NÃO é o canhoto da NF, e sim a folha de CANHOTEIRA do
romaneio (uma tabela impressa com campos de assinatura por entrega) ou documento
equivalente assinado por quem recebeu. Aplique os MESMOS critérios de legibilidade,
assinatura/identificação do recebedor e enquadramento a esse documento -- não reprove
por "documento errado" só porque não é um canhoto de NF."""


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
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extrair_checklist(servico: dict) -> dict | None:
    respostas = (servico.get("checklistAnswers") or {}).get("data") or []
    return respostas[0] if respostas else None


def buscar_checklists_pendentes(token: str, horas: int) -> list:
    """
    Serviços entregues com sucesso (status_done='success', code PS-*)
    na janela de horas, com checklist PREENCHIDO e validated_at=null.
    Mesma busca paginada do expedir_pedidos.py.
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
            if s.get("status") != "done" or s.get("status_done") != "success":
                continue
            if not re.match(r"#?PS-\d+", s.get("code", ""), re.IGNORECASE):
                continue
            cl = _extrair_checklist(s)
            if cl is None or cl.get("validated_at"):
                continue
            encontrados.append(s)

        if parar:
            break
        pag = body.get("meta", {}).get("pagination", {})
        if page >= pag.get("total_pages", page):
            break
        page += 1
        time.sleep(0.3)

    logger.info(f"VUUPT: {len(encontrados)} checklist(s) pendente(s) de validacao nas ultimas {horas}h")
    return encontrados


def baixar_canhoto_pdf(token: str, checklist_id: int, codigo_ps: str) -> Path | None:
    """Mesmo cache dados/canhotos/ do expedir_pedidos.py."""
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


def validar_checklist_no_vuupt(token: str, checklist_id: int) -> bool:
    """
    PUT /checklists/{id}/validate -- preenche validated_at, o mesmo
    efeito da validação manual na tela do VUUPT (rota descoberta em
    13/08: PUT é o único método aceito nela).
    """
    try:
        resp = requests.put(
            f"{VUUPT_CHECK}/{checklist_id}/validate",
            headers=_headers_vuupt(token), json={}, timeout=30,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"  Falha ao validar checklist {checklist_id} no VUUPT: {e}")
        return False


def duplicar_servico_por_canhoto(vuupt: VuuptClient, servico_original: dict) -> dict | None:
    """
    Clona o pedido no VUUPT pra RECOLETA DE CANHOTO quando a foto foi
    reprovada -- mesmo mecanismo do duplicar_servico_por_insucesso do
    expedir_pedidos.py, com sufixo -C (canhoto) pra não confundir com
    as reentregas -R de insucesso. O novo nasce not_assigned e entra no
    próximo ciclo de roteirização normalmente.
    """
    code_original = (servico_original.get("code") or "").lstrip("#")
    sufixo = 1
    novo_code = f"{code_original}-C{sufixo}"
    while vuupt.buscar_servico_por_code(novo_code):
        sufixo += 1
        novo_code = f"{code_original}-C{sufixo}"

    payload = {
        "code": novo_code,
        "title": f"{servico_original.get('title', '')} (recoleta canhoto)",
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
        logger.info(f"  Pedido clonado p/ recoleta: {servico_original.get('code')} -> {novo_code}")
        # A resposta de criação do VUUPT não ecoa 'code' na raiz (achado
        # 12/08) -- o code correto é o que acabou de ir no payload.
        return {**(novo_servico or {}), "code": novo_code}
    except Exception as e:
        logger.error(f"  Falha ao clonar {servico_original.get('code')}: {e}")
        return None


# ── Claude (visão) ────────────────────────────────────────────────────────────

def _extrair_fotos_do_pdf(pdf_path: Path, max_fotos: int = 5) -> list[dict]:
    """
    Extrai as fotos embutidas no PDF do /print em resolução ORIGINAL
    (a foto do motorista vai a ~2000px, mas renderizada na página do
    PDF ela encolhe e vira falso "ilegível" -- visto no teste de
    13/08). Filtra logotipos/miniaturas (pequenos) e deduplica.
    Retorna blocos de conteúdo 'image' prontos pra API; [] se falhar
    (a análise segue só com o PDF).
    """
    try:
        from PIL import Image
        from pypdf import PdfReader

        blocos, vistos = [], set()
        for page in PdfReader(str(pdf_path)).pages:
            for img in page.images:
                dados = img.data
                digest = hashlib.sha1(dados).hexdigest()
                if digest in vistos:
                    continue
                vistos.add(digest)
                try:
                    im = Image.open(io.BytesIO(dados))
                except Exception:
                    continue
                w, h = im.size
                if min(w, h) < 300 or len(dados) < 30_000:
                    continue  # logotipo, miniatura de mapa, assinatura digitalizada do rodapé
                media = {"JPEG": "image/jpeg", "PNG": "image/png",
                         "WEBP": "image/webp", "GIF": "image/gif"}.get(im.format)
                if media is None:
                    continue
                blocos.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media,
                        "data": base64.standard_b64encode(dados).decode("ascii"),
                    },
                })
                if len(blocos) >= max_fotos:
                    return blocos
        return blocos
    except Exception as e:
        logger.warning(f"  Falha ao extrair fotos de {pdf_path.name}: {e} -- segue so com o PDF.")
        return []


def analisar_canhoto_via_claude(pdf_path: Path, api_key: str, sem_nf: bool = False) -> dict | None:
    """
    Manda o PDF do canhoto pra visão do Claude e retorna
    {"decisao": "aprovada"|"reprovada"|"duvida", "motivo": str},
    ou None se a chamada falhar (quem chama decide: pula e tenta na
    próxima execução, sem gravar fingerprint).

    sem_nf: entrega de embarcador SEM Nota Fiscal (SENDERS_SEM_NF) --
    o documento válido passa a ser a canhoteira do romaneio.

    fallbacks="default": se o classificador de segurança do modelo
    recusar (falso positivo raro), a própria API reexecuta no modelo
    reserva recomendado em vez de devolver a recusa.
    """
    prompt  = _PROMPT_VALIDACAO + (_CONTEXTO_SEM_NF if sem_nf else "")
    pdf_b64 = base64.standard_b64encode(pdf_path.read_bytes()).decode("ascii")
    fotos   = _extrair_fotos_do_pdf(pdf_path)
    if fotos:
        logger.info(f"  {len(fotos)} foto(s) em alta resolucao extraida(s) do PDF.")
    try:
        resp = requests.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "server-side-fallback-2026-07-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 1024,
                "fallbacks": "default",
                "output_config": {
                    "format": {"type": "json_schema", "schema": _SCHEMA_DECISAO},
                },
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        *fotos,
                        {"type": "text", "text": prompt},
                    ],
                }],
            },
            timeout=180,
        )
        resp.raise_for_status()
        body = resp.json()

        if body.get("stop_reason") == "refusal":
            # Toda a cadeia (modelo + reserva) recusou -- trata como caso
            # de revisão humana, nunca aprova/reprova às cegas.
            logger.warning(f"  Claude recusou analisar {pdf_path.name} -- vai pra revisao manual.")
            return {"decisao": "duvida", "motivo": "Análise automática indisponível (recusa do modelo)."}

        texto = next(b["text"] for b in body.get("content", []) if b.get("type") == "text")
        return json.loads(texto)
    except Exception as e:
        logger.error(f"  Erro na analise via Claude de {pdf_path.name}: {e}")
        return None


# ── Notificação ───────────────────────────────────────────────────────────────

def _linhas_tabela(itens: list[dict], com_clone: bool) -> str:
    linhas = ""
    for item in itens:
        clone_td = ""
        if com_clone:
            novo = item.get("novo_code") or "falha ao clonar"
            cor  = "#1F2937" if item.get("novo_code") else "#EF4444"
            clone_td = (
                f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
                f"font-weight:600;color:{cor};'>{html.escape(novo)}</td>"
            )
        linhas += (
            f"<tr>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-weight:600;"
            f"font-size:13px;color:#1F2937;'>{html.escape(item.get('code', ''))}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#6B7280;'>{html.escape(item.get('title', '') or '')}</td>"
            f"<td style='padding:8px 14px;border-bottom:1px solid #E5E7EB;font-size:12px;"
            f"color:#B45309;'>{html.escape(item.get('motivo', '') or '')}</td>"
            f"{clone_td}"
            f"</tr>"
        )
    return linhas


def notificar_resultado(reprovados: list[dict], duvidas: list[dict],
                        config_email: dict, modo_teste: bool):
    """
    E-mail interno com as fotos REPROVADAS (pedido já clonado pra
    recoleta) e os casos EM DÚVIDA (revisão manual no VUUPT). Mesmo
    padrão visual das notificações do expedir_pedidos.py.
    """
    if not reprovados and not duvidas:
        return

    remetente   = config_email.get("remetente", "")
    senha       = config_email.get("senha_app") or config_email.get("senha", "")
    destino     = config_email.get("email_responsavel", "")
    if not destino:
        logger.info("E-mail de responsavel nao configurado -- notificacao pulada.")
        return

    COR_HEADER = "#141428"
    COR_ERRO   = "#EF4444"
    COR_AVISO  = "#F5A623"

    blocos = ""
    if reprovados:
        blocos += f"""
<h3 style="color:{COR_ERRO};margin:18px 0 6px;">Fotos reprovadas — pedido clonado p/ recoleta</h3>
<p style="color:#6B7280;font-size:12px;margin:0 0 8px;">O clone entra na próxima roteirização.
O pedido original continua pendente de validação manual no VUUPT.</p>
<table style="border-collapse:collapse;width:100%;background:#fff;">
<tr>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Pedido</th>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Cliente</th>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Motivo</th>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Clone</th>
</tr>
{_linhas_tabela(reprovados, com_clone=True)}
</table>"""
    if duvidas:
        blocos += f"""
<h3 style="color:{COR_AVISO};margin:18px 0 6px;">Casos em dúvida — revisar manualmente no VUUPT</h3>
<p style="color:#6B7280;font-size:12px;margin:0 0 8px;">O agente não teve confiança pra aprovar
nem reprovar. Validar (ou não) direto na tela de checklists do VUUPT.</p>
<table style="border-collapse:collapse;width:100%;background:#fff;">
<tr>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Pedido</th>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Cliente</th>
<th style="text-align:left;padding:8px 14px;font-size:11px;color:#6B7280;">Motivo</th>
</tr>
{_linhas_tabela(duvidas, com_clone=False)}
</table>"""

    corpo = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;">
<div style="background:{COR_HEADER};padding:20px;border-radius:8px 8px 0 0;">
<h2 style="color:#fff;margin:0;">Validação de Checklists — Canhotos fora do padrão</h2></div>
<div style="background:#f9f9f9;padding:24px;border:1px solid #E5E7EB;border-top:none;border-radius:0 0 8px 8px;">
{blocos}
<p style="color:#9CA3AF;font-size:11px;margin-top:18px;">Agente de validação de checklists —
{datetime.now().strftime('%d/%m/%Y %H:%M')}</p>
</div></body></html>"""

    prefixo = "[TESTE] " if modo_teste else ""
    assunto = (f"{prefixo}Checklists: {len(reprovados)} reprovado(s), "
               f"{len(duvidas)} em dúvida — {datetime.now().strftime('%d/%m %H:%M')}")

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
        logger.info(f"  Notificacao enviada para {destino}")
    except Exception as e:
        logger.warning(f"  Falha ao enviar notificacao: {e}")


# ── Fluxo principal ───────────────────────────────────────────────────────────

def main(horas: int = HORAS_PADRAO, limite: int | None = None, modo_teste: bool = False):
    logger.info("=" * 60)
    logger.info(f"Validacao de checklists iniciada (janela {horas}h"
                f"{', MODO TESTE' if modo_teste else ''})")

    config = _carregar_config()
    vuupt_token = (config.get("vuupt_api") or {}).get("token", "")
    api_key     = (config.get("anthropic") or {}).get("api_key", "")
    config_email = config.get("email") or {}

    if not vuupt_token:
        logger.error("Token do VUUPT nao configurado em config.yaml (vuupt_api.token).")
        return
    if not api_key:
        logger.error("Chave da API Anthropic nao configurada em config.yaml (secao anthropic).")
        return

    servicos = buscar_checklists_pendentes(vuupt_token, horas)
    novos = []
    for s in servicos:
        cl = _extrair_checklist(s)
        if cl and not fingerprint_validacao.ja_analisado(cl["id"]):
            novos.append(s)
    logger.info(f"{len(novos)} checklist(s) ainda nao analisado(s) pelo agente.")

    if limite and len(novos) > limite:
        novos = novos[:limite]
        logger.info(f"Limite: processando {limite} checklist(s).")

    if not novos:
        logger.info("Nada a fazer.")
        return

    vuupt = VuuptClient(vuupt_token)
    aprovados, reprovados, duvidas, erros = [], [], [], 0

    for i, servico in enumerate(novos, 1):
        cl           = _extrair_checklist(servico)
        checklist_id = cl["id"]
        code         = (servico.get("code") or "").lstrip("#")
        title        = servico.get("title", "")
        logger.info(f"[{i}/{len(novos)}] {code} | checklist_id={checklist_id} | "
                    f"fotos={cl.get('images_quantity')}")

        try:
            # Checklist preenchido sem NENHUMA foto: fora do padrão por
            # definição (nunca teria como aprovar) -- reprova direto,
            # sem gastar análise de visão.
            if int(cl.get("images_quantity") or 0) == 0:
                resultado = {"decisao": "reprovada",
                             "motivo": "Checklist preenchido sem nenhuma foto do canhoto."}
            else:
                pdf_path = baixar_canhoto_pdf(vuupt_token, checklist_id, code)
                if pdf_path is None:
                    erros += 1
                    continue  # sem fingerprint -- tenta de novo na próxima execução
                resultado = analisar_canhoto_via_claude(
                    pdf_path, api_key,
                    sem_nf=servico.get("sender_id") in SENDERS_SEM_NF)
                if resultado is None:
                    erros += 1
                    continue  # idem

            decisao = resultado["decisao"]
            motivo  = resultado.get("motivo", "")
            logger.info(f"  Decisao: {decisao.upper()} -- {motivo}")

            if modo_teste:
                # Só contabiliza pro resumo -- nenhuma ação real.
                if decisao == "aprovada":
                    aprovados.append(code)
                elif decisao == "reprovada":
                    reprovados.append({"code": code, "title": title, "motivo": motivo,
                                       "novo_code": None})
                else:
                    duvidas.append({"code": code, "title": title, "motivo": motivo})
                continue

            if decisao == "aprovada":
                if validar_checklist_no_vuupt(vuupt_token, checklist_id):
                    logger.info(f"  Checklist {checklist_id} validado no VUUPT.")
                    fingerprint_validacao.registrar(checklist_id, servico.get("id"), code,
                                                    "aprovada", motivo)
                    aprovados.append(code)
                else:
                    erros += 1  # sem fingerprint -- reanalisa/revalida na próxima

            elif decisao == "reprovada":
                novo = duplicar_servico_por_canhoto(vuupt, servico)
                if novo is None:
                    erros += 1
                    continue  # sem fingerprint -- tenta clonar de novo na próxima
                fingerprint_validacao.registrar(checklist_id, servico.get("id"), code,
                                                "reprovada", motivo, novo_code=novo["code"])
                reprovados.append({"code": code, "title": title, "motivo": motivo,
                                   "novo_code": novo["code"]})

            else:  # duvida
                fingerprint_validacao.registrar(checklist_id, servico.get("id"), code,
                                                "duvida", motivo)
                duvidas.append({"code": code, "title": title, "motivo": motivo})

        except Exception as e:
            erros += 1
            logger.error(f"  Erro inesperado em {code}: {e}")

    if not modo_teste:
        notificar_resultado(reprovados, duvidas, config_email, modo_teste)

    logger.info(f"Resumo: {len(aprovados)} aprovado(s), {len(reprovados)} reprovado(s), "
                f"{len(duvidas)} em duvida, {erros} erro(s).")
    logger.info("Validacao de checklists finalizada.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Validacao automatica dos checklists (canhotos) no VUUPT")
    parser.add_argument("--horas", type=int, default=HORAS_PADRAO,
                        help=f"Janela de busca em horas (padrao {HORAS_PADRAO})")
    parser.add_argument("--limite", type=int, default=None,
                        help="Maximo de checklists a analisar nesta execucao")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Analisa e loga as decisoes, mas nao valida, nao clona e nao envia e-mail")
    args = parser.parse_args()
    main(horas=args.horas, limite=args.limite, modo_teste=args.modo_teste)
