# -*- coding: utf-8 -*-
"""
notificar_pedidos_em_espera.py

Script paralelo ao pipeline principal.

Fluxo:
  1. Busca pedidos 'On hold' (Em espera) no Stokki
  2. Faturados (marker com 'faturado'): processa automaticamente via
     Estacao de Impressao -> muda para 'Aguardando Transportador'
  3. Aguardando Faturamento: envia e-mail para o embarcador

Execute:
  py -3.11 notificar_pedidos_em_espera.py
  py -3.11 notificar_pedidos_em_espera.py --modo-teste   (envia para hugo@freshlogbr.com)
"""
import argparse
import html
import logging
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

import yaml

from notificar_execucao_agente import notificar_execucao

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from email_utils import enviar_email
from stokki.auth import StokkiSession
from stokki import pedidos as stokki_pedidos
from stokki.estacao_impressao import imprimir_pedidos_pendentes

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("notificador")

CONFIG_PATH  = _RAIZ / "config.yaml"
DB_PATH      = _RAIZ / "dados" / "dados.db"
EMAIL_TESTE  = "hugo@freshlogbr.com"

STATUS_EM_ESPERA_API = "On hold"
MARKER_NOTIFICAR     = "aguardando faturamento"
MARKER_IGNORAR       = "faturado"

# Paleta Freshlog (do notificador_email.py do agente_relatorio)
COR_PRIMARIA       = "#141428"
COR_PRIMARIA_CLARA = "#E6FBF5"
COR_ACENTO         = "#00C896"
COR_DESTAQUE       = "#F5A623"
COR_TEXTO          = "#1F2937"
COR_TEXTO_SUAVE    = "#6B7280"
COR_BORDA          = "#E5E7EB"
COR_FUNDO          = "#F4F6F5"


def _carregar_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _carregar_embarcadores_do_banco():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Banco nao encontrado: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT stkkc_id, nome_remetente, apelido, email, notificar_email "
        "FROM interno WHERE stkkc_id IS NOT NULL"
    ).fetchall()
    conn.close()
    embs = {}
    for r in rows:
        raw = r["email"] or ""
        emails = [e.strip() for e in re.split(r"[,;\t]+", raw) if e.strip() and "@" in e]
        embs[r["stkkc_id"]] = {
            "stkkc_id":  r["stkkc_id"],
            "nome":      r["apelido"] or r["nome_remetente"] or "",
            "emails":    emails,
        }
    logger.info(f"Banco: {len(embs)} embarcadores com stkkc_id")
    return embs


def _normalizar(s):
    return re.sub(r"<[^>]+>", "", s).strip()


def _extrair_marker(linha):
    return _normalizar(str(linha.get("marker", "") if isinstance(linha, dict) else ""))

def _extrair_state(linha):
    return _normalizar(str(linha.get("state", "") if isinstance(linha, dict) else ""))

def _extrair_codigo_ps(linha):
    id_html = str(linha.get("id", "") if isinstance(linha, dict) else "")
    m = re.search(r"#PS-(\d+)", id_html)
    return f"#PS-{m.group(1)}" if m else ""

def _extrair_id_stokki(linha):
    for campo in (linha.values() if isinstance(linha, dict) else linha):
        m = re.search(r"/show/(\d+)", str(campo))
        if m:
            return int(m.group(1))
    return None

def _extrair_data(linha):
    return str(linha.get("expedition_date", "") if isinstance(linha, dict) else "")

def _extrair_destino(linha):
    dest = str(linha.get("destination", "") if isinstance(linha, dict) else "")
    return _normalizar(dest).split("\n")[0]

def _extrair_stkkc_id(linha):
    client_html = str(linha.get("client", "") if isinstance(linha, dict) else "")
    m = re.search(r"#stkkc-(\d+)", client_html)
    return int(m.group(1)) if m else None


def _buscar_pedidos_em_espera(sess):
    logger.info("Buscando pedidos 'On hold' (Em espera)...")
    todos = []
    for linha in stokki_pedidos.iterar_todos_pedidos(sess, status=STATUS_EM_ESPERA_API, pausa_entre_paginas=0.3):
        marker = _extrair_marker(linha)
        if MARKER_NOTIFICAR not in marker.lower() and MARKER_IGNORAR not in marker.lower():
            continue
        todos.append({
            "codigo_ps": _extrair_codigo_ps(linha),
            "id_stokki": _extrair_id_stokki(linha),
            "data":      _extrair_data(linha),
            "destino":   _extrair_destino(linha),
            "marker":    marker,
            "stkkc_id":  _extrair_stkkc_id(linha),
        })
    logger.info(f"Em espera relevantes: {len(todos)}")
    return todos


def _envelope_html(conteudo, rodape="Esta e uma mensagem automatica da Freshlog."):
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:{COR_FUNDO};font-family:'Segoe UI',Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:{COR_FUNDO};padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;width:100%;background-color:#FFFFFF;border-radius:10px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
        <tr>
          <td style="background-color:#FFFFFF;padding:24px 32px;border-bottom:1px solid {COR_BORDA};">
            <img src="cid:logo_freshlog" alt="Freshlog Logistica" width="150" style="display:block;border:0;">
          </td>
        </tr>
        <tr><td style="background-color:{COR_ACENTO};height:4px;line-height:4px;font-size:0;">&nbsp;</td></tr>
        <tr><td style="padding:32px;">{conteudo}</td></tr>
        <tr>
          <td style="padding:20px 32px;background-color:{COR_FUNDO};border-top:1px solid {COR_BORDA};">
            <p style="margin:0;font-size:12px;color:{COR_TEXTO_SUAVE};line-height:1.6;">
              {rodape}<br>Por favor, responda este e-mail mantendo o historico da conversa.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body></html>"""


def _montar_corpo(nome_emb, pedidos):
    linhas = ""
    for p in pedidos:
        linhas += (
            f"<tr>"
            f"<td style='padding:10px 14px;border-bottom:1px solid {COR_BORDA};font-size:13px;"
            f"color:{COR_TEXTO};font-weight:600;'>{html.escape(str(p['codigo_ps']))}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid {COR_BORDA};font-size:12px;"
            f"color:{COR_TEXTO_SUAVE};'>{html.escape(str(p['destino']))}</td>"
            f"<td style='padding:10px 14px;border-bottom:1px solid {COR_BORDA};font-size:12px;"
            f"color:{COR_TEXTO_SUAVE};'>{html.escape(str(p['data']))}</td>"
            f"</tr>"
        )
    conteudo = f"""
    <h2 style="margin:0 0 4px;font-size:20px;color:{COR_TEXTO};">Pedidos aguardando envio do XML</h2>
    <p style="margin:0 0 24px;font-size:14px;color:{COR_TEXTO_SUAVE};">
      Ola, <strong>{html.escape(str(nome_emb))}</strong>!<br><br>
      Os pedidos abaixo estao em espera aguardando o XML da Nota Fiscal Eletronica.
      Por favor, <strong>envie o XML pelo portal</strong> o mais breve possivel para que
      possamos dar continuidade a expedicao.
    </p>
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="border:1px solid {COR_BORDA};border-top:2px solid {COR_DESTAQUE};border-radius:8px;overflow:hidden;margin-bottom:24px;">
      <tr style="background-color:{COR_PRIMARIA_CLARA};">
        <td style="padding:10px 14px;font-size:12px;font-weight:600;color:{COR_PRIMARIA};text-transform:uppercase;letter-spacing:0.4px;">Pedido</td>
        <td style="padding:10px 14px;font-size:12px;font-weight:600;color:{COR_PRIMARIA};text-transform:uppercase;letter-spacing:0.4px;">Destinatario</td>
        <td style="padding:10px 14px;font-size:12px;font-weight:600;color:{COR_PRIMARIA};text-transform:uppercase;letter-spacing:0.4px;">Data Saida</td>
      </tr>
      {linhas}
    </table>
    <p style="margin:0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
      Atenciosamente,<br><strong>Freshlog Logistica</strong>
    </p>"""
    return _envelope_html(conteudo, "Freshlog Logistica - solicitacao automatica de XML de NF-e.")




def main(modo_teste=False):
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Notificador iniciado.")
    inicio = time.time()
    resultado = {"status": "ok", "detalhe": ""}
    config = {}

    try:
        config       = _carregar_config()
        config_email = config.get("email", {})
        embarcadores = _carregar_embarcadores_do_banco()

        sess           = StokkiSession(config)
        pedidos_espera = _buscar_pedidos_em_espera(sess)

        if not pedidos_espera:
            logger.info("Nenhum pedido em espera relevante.")
            resultado["detalhe"] = "Nenhum pedido em espera relevante."
            return

        faturados   = [p for p in pedidos_espera if MARKER_IGNORAR in p.get("marker", "").lower()]
        a_notificar = [p for p in pedidos_espera if p not in faturados]

        detalhe_impressao = ""
        # Processa faturados automaticamente (ignora notificar_email)
        if faturados:
            logger.info(
                f"{len(faturados)} pedido(s) com marker 'faturado' detectado(s) -- "
                f"acionando a Estacao de Impressao (processa a fila inteira: a "
                f"propria fila la ja E o conjunto de pedidos faturados prontos, "
                f"nao ha correspondencia 1-a-1 por codigo a fazer aqui)."
            )
            resultado_impressao = imprimir_pedidos_pendentes(config, dry_run=modo_teste)
            if modo_teste:
                logger.info(
                    f"  [TESTE] {resultado_impressao['pendentes']} pedido(s) na fila "
                    f"da Estacao de Impressao (nenhum clique realizado)."
                )
                detalhe_impressao = f"{resultado_impressao['pendentes']} faturado(s) na fila (teste)"
            else:
                logger.info(
                    f"  Processados: {resultado_impressao['processados']}/"
                    f"{resultado_impressao['pendentes']} pedido(s) na fila."
                )
                detalhe_impressao = (
                    f"{resultado_impressao['processados']}/{resultado_impressao['pendentes']} faturado(s) processado(s)"
                )
                if resultado_impressao["falhas"]:
                    logger.warning(
                        f"  {len(resultado_impressao['falhas'])} falha(s) "
                        f"(popup nao fechou): {resultado_impressao['falhas']}"
                    )
                    detalhe_impressao += f", {len(resultado_impressao['falhas'])} falha(s)"

        if not a_notificar:
            logger.info("Nenhum pedido para notificar por e-mail.")
            resultado["detalhe"] = (
                (detalhe_impressao + "; " if detalhe_impressao else "") + "Nenhum pedido para notificar por e-mail."
            )
            return

        # Agrupa por stkkc_id
        grupos = defaultdict(list)
        sem_stkkc = []
        for p in a_notificar:
            if p["stkkc_id"] and p["stkkc_id"] in embarcadores:
                grupos[p["stkkc_id"]].append(p)
            else:
                sem_stkkc.append(p)

        if sem_stkkc:
            logger.info(f"{len(sem_stkkc)} pedido(s) sem embarcador no banco:")
            for p in sem_stkkc:
                logger.info(f"  {p['codigo_ps']}")

        enviados = falhas = sem_email = 0

        for stkkc_id, pedidos in grupos.items():
            emb    = embarcadores[stkkc_id]
            nome   = emb["nome"]
            emails = emb["emails"]
            qtd    = len(pedidos)

            if not emails:
                logger.warning(f"  {nome}: {qtd} pedido(s) -- sem e-mail cadastrado.")
                sem_email += 1
                continue

            assunto = f"[Freshlog] {qtd} pedido(s) aguardando envio do XML"
            corpo   = _montar_corpo(nome, pedidos)

            destinos = [EMAIL_TESTE] if modo_teste else emails

            if modo_teste:
                logger.info(
                    f"  [TESTE] {nome} -> {EMAIL_TESTE} "
                    f"(original: {emails}) | {qtd} pedido(s): {[p['codigo_ps'] for p in pedidos]}"
                )

            if enviar_email(destinos, assunto, corpo, config_email):
                if not modo_teste:
                    logger.info(f"  OK: {nome} -> {', '.join(emails)}")
                else:
                    logger.info(f"    OK: e-mail de teste enviado para {EMAIL_TESTE}")
                enviados += 1
            else:
                falhas += 1

        logger.info("=" * 60)
        logger.info(
            f"RESUMO {'(MODO TESTE) ' if modo_teste else ''}"
            f"Enviados: {enviados} | Falhas: {falhas} | Sem e-mail: {sem_email}"
        )
        partes = []
        if detalhe_impressao:
            partes.append(detalhe_impressao)
        partes.append(f"{enviados} enviado(s), {falhas} falha(s), {sem_email} sem e-mail")
        if sem_stkkc:
            partes.append(f"{len(sem_stkkc)} sem embarcador cadastrado")
        resultado["detalhe"] = "; ".join(partes)
        if falhas:
            resultado["status"] = "erro"

    except Exception as e:
        logger.exception(f"Erro no notificador: {e}")
        resultado["status"] = "erro"
        resultado["detalhe"] = str(e)
    finally:
        try:
            if not config:
                config = _carregar_config()
            duracao = time.time() - inicio
            notificar_execucao({"Notificador de Faturamento": resultado}, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--modo-teste", action="store_true")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
