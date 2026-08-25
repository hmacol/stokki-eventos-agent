# -*- coding: utf-8 -*-
"""
notificacao_transportadoras/notificar_transportadoras.py

Notifica cada transportadora de redespacho (tipo TERCEIROS em
BD_TRANSPORTADORAS.xlsx) sobre os pedidos do dia que serão entregues pra
ela, anexando o XML da NF-e de cada um, com cópia pro(s) embarcador(es)
(remetente da NF) -- pedido do Hugo, 13/08. Ver doc de origem
DOC_EXECUCAO_CLAUDE_NOTIFICACAO_TRANSPORTADORAS.md.

Fluxo:
  1. Rotas do dia no VUUPT (mesmo buscar_rotas_do_dia de
     gerar_pdf_romaneios.py/avisar_motoristas_rotas.py) -- todo pedido
     roteirizado pra hoje.
  2. Pra cada pedido, confere o bloco "Transportadora" na Stokki
     (stokki_pedidos.obter_detalhe -- leve, via requests, não Playwright)
     e resolve contra o catálogo (regras/transportadoras.py). Só entra
     nesta notificação quem resolve como tipo TERCEIROS -- mesma regra
     que pipeline.py já usa pra decidir o redespacho na importação.
  3. Agrupa por transportadora, descarta pedidos já notificados hoje
     (fingerprint_notificacao_transportadora.py -- rate-limit incremental:
     só os pedidos NOVOS do grupo geram e-mail, os já enviados hoje não
     reentram).
  4. Baixa o XML de cada pedido novo (Playwright, só pros pedidos que
     sobraram -- ver documentos_pedido/stokki_documentos.py::baixar_xml_nfe).
  5. Envia 1 e-mail por transportadora (To = e-mail da transportadora,
     Cc = e-mail(s) do(s) embarcador(es) dos pedidos do grupo), com os
     XMLs anexados.

COMO USAR:
    py -3.11 notificacao_transportadoras/notificar_transportadoras.py --modo-teste --data hoje
    py -3.11 notificacao_transportadoras/notificar_transportadoras.py --data hoje
"""
import argparse
import html
import logging
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

_RAIZ_LOCAL   = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
# append (não insert): pastas de outras features entram por último pra
# não sombrear nenhum módulo da raiz -- mesmo cuidado de gerar_pdf_romaneios.py.
sys.path.append(str(_RAIZ_PROJETO / "roteirizacao"))
sys.path.append(str(_RAIZ_PROJETO / "documentos_pedido"))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "notificar_transportadoras.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("notificar_transportadoras")

import yaml

from avisar_motoristas_rotas import _extrair_servicos_da_rota, _parse_data, buscar_rotas_do_dia
from email_utils import (
    COR_BORDA, COR_FUNDO, COR_PRIMARIA, COR_TEXTO, envelope_html, enviar_email, notificacoes_automaticas_ativas,
)
from notificar_execucao_agente import notificar_execucao
from regras.transportadoras import CatalogoTransportadoras
from stokki import pedidos as stokki_pedidos
from stokki.auth import StokkiSession

from fingerprint_notificacao_transportadora import pedidos_ja_notificados_hoje, registrar_notificacao

TRANSPORTADORAS = _RAIZ_PROJETO / "dados" / "BD_TRANSPORTADORAS.xlsx"
DB_PATH = _RAIZ_PROJETO / "dados" / "dados.db"
EMAIL_TESTE = "hugo@freshlogbr.com"


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _id_stokki(codigo_ps: str) -> int | None:
    """'PS-36327' -> 36327. Primeira sequência de dígitos, não a última --
    mesmo cuidado de documentos_pedido/stokki_documentos.py::_extrair_id
    (reentrega tem sufixo '-R1' que não pode virar o ID)."""
    m = re.search(r"(\d+)", codigo_ps or "")
    return int(m.group(1)) if m else None


def _carregar_embarcadores_por_sender_id() -> dict:
    """Mesmo padrão de roteirizacao/notificar_agendamento_dia_fixo.py --
    sender_id -> {"nome", "emails"} a partir da tabela interno."""
    if not DB_PATH.exists():
        raise FileNotFoundError(f"Banco não encontrado: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT sender_id, nome_remetente, apelido, email FROM interno WHERE sender_id IS NOT NULL"
    ).fetchall()
    conn.close()
    embs = {}
    for r in rows:
        raw = r["email"] or ""
        emails = [e.strip() for e in re.split(r"[,;\t]+", raw) if e.strip() and "@" in e]
        embs[r["sender_id"]] = {"nome": r["apelido"] or r["nome_remetente"] or "", "emails": emails}
    return embs


def _tag_local(el) -> str:
    return el.tag.split("}")[-1]


def _extrair_nnf(caminho_xml: Path) -> str:
    """Número da NF-e (tag <nNF>) do XML baixado -- namespace-agnóstico,
    mesmo padrão de telefone_origem.py::indexar_xmls. String vazia se não
    achar (não trava o envio -- é só informativo na tabela do e-mail)."""
    try:
        tree = ET.parse(caminho_xml)
    except ET.ParseError:
        return ""
    for el in tree.iter():
        if _tag_local(el) == "nNF":
            return (el.text or "").strip()
    return ""


def identificar_pedidos_transportadora(rotas: list[dict], sess_stokki: StokkiSession,
                                       catalogo: CatalogoTransportadoras) -> tuple[dict, dict]:
    """
    Varre os serviços das rotas do dia e reconfirma, pedido a pedido, contra
    o bloco "Transportadora" da Stokki -- mesma lógica de pipeline.py na
    importação (não dá pra confiar só no endereço do serviço no VUUPT, que
    já veio substituído pelo endereço de redespacho e não identifica QUAL
    transportadora é).

    Retorna (grupos, detalhes):
      grupos: {nome_normalizado_transportadora: [codigo_ps, ...]}
      detalhes: {codigo_ps: {"sender_id", "nome_transportadora", "email_transportadora"}}
    """
    grupos: dict[str, list[str]] = defaultdict(list)
    detalhes: dict[str, dict] = {}

    for rota in rotas:
        for s in _extrair_servicos_da_rota(rota):
            codigo = (s.get("code") or "").lstrip("#")
            id_stokki = _id_stokki(codigo)
            if not codigo or id_stokki is None:
                continue

            try:
                detalhe = stokki_pedidos.obter_detalhe(sess_stokki, id_stokki)
            except Exception as e:
                logger.warning(f"  {codigo}: falha ao buscar detalhe na Stokki -- {e}")
                continue

            transp = detalhe.get("transportadora") or {}
            nome_transp = transp.get("nome", "")
            if not nome_transp:
                continue

            resultado = catalogo.resolver(nome_transp, transp.get("documento", ""))
            if resultado.tipo != "TERCEIROS":
                continue

            chave = resultado.nome_normalizado
            grupos[chave].append(codigo)
            detalhes[codigo] = {
                "sender_id": s.get("sender_id"),
                "nome_transportadora": resultado.nome_original or nome_transp,
                "email_transportadora": resultado.email or transp.get("email", ""),
            }

    return dict(grupos), detalhes


def _montar_conteudo(nome_transportadora: str, data_br: str, itens: list[dict]) -> str:
    linhas = "".join(f"""
    <tr>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(i['codigo'])}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(i['nf'] or '—')}</td>
      <td style="padding:8px 14px;border-bottom:1px solid {COR_BORDA};">{html.escape(i['embarcador'] or '—')}</td>
    </tr>""" for i in itens)

    return f"""
<p style="margin:0 0 4px 0;font-size:12px;font-weight:800;color:{COR_PRIMARIA};letter-spacing:0.5px;">
  ENTREGA DO DIA {html.escape(data_br)}
</p>
<p style="margin:0 0 16px 0;font-size:20px;font-weight:800;color:{COR_PRIMARIA};">
  Notas fiscais para entrega — {html.escape(nome_transportadora)}
</p>
<p style="margin:0 0 20px 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Olá! Os pedidos abaixo estão programados para entrega a vocês hoje
  (<strong>{html.escape(data_br)}</strong>). O XML da NF-e de cada um vai anexado
  a este e-mail.
</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
      style="border:1px solid {COR_BORDA};border-radius:8px;overflow:hidden;">
<thead><tr style="background:{COR_FUNDO};">
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Pedido</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">NF</th>
<th style="padding:8px 14px;text-align:left;font-size:11px;color:{COR_PRIMARIA};">Embarcador</th>
</tr></thead><tbody>{linhas}</tbody></table>
<p style="margin:20px 0 0 0;font-size:14px;color:{COR_TEXTO};line-height:1.6;">
  Qualquer dúvida, basta responder este e-mail.<br><br>
  Atenciosamente,<br><strong>Freshlog Logística</strong>
</p>
"""


def main(modo_teste: bool, data_str: str) -> int:
    inicio = time.time()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    config_email = config.get("email", {})

    catalogo = CatalogoTransportadoras.carregar(TRANSPORTADORAS)
    sess_stokki = StokkiSession(config)

    data_alvo = _parse_data(data_str)
    data_br = data_alvo.strftime("%d/%m/%Y")
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Notificação de transportadoras para {data_br}.")

    rotas = buscar_rotas_do_dia(token, data_alvo)
    logger.info(f"{len(rotas)} rota(s) para {data_br}.")

    grupos, detalhes = identificar_pedidos_transportadora(rotas, sess_stokki, catalogo)
    total_pedidos = sum(len(v) for v in grupos.values())
    logger.info(f"{len(grupos)} transportadora(s) com entrega(s) hoje ({total_pedidos} pedido(s)).")

    resumo_etapas: dict = {}
    contadores = {"transportadoras_notificadas": 0, "pedidos_enviados": 0,
                 "sem_email": 0, "sem_xml": 0, "falhas": 0}

    if not notificacoes_automaticas_ativas(config):
        resumo_etapas["Resumo geral"] = {
            "status": "ok",
            "detalhe": "Notificação automática desativada (config.yaml: notificacoes_automaticas.ativo=false).",
        }
        logger.info("Notificações automáticas desativadas -- pulando notificação de transportadoras.")
    elif not grupos:
        resumo_etapas["Resumo geral"] = {"status": "ok", "detalhe": "Nenhuma entrega para transportadora hoje."}
        logger.info("Nenhuma entrega para transportadora hoje -- nada a notificar.")
    else:
        embarcadores = _carregar_embarcadores_por_sender_id()

        from playwright.sync_api import sync_playwright
        from stokki_documentos import URL_PROVIDER_SHW, _extrair_id, _login, baixar_xml_nfe, nova_pagina

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = nova_pagina(browser)
            _login(page, config)

            for chave_transp, codigos in grupos.items():
                ja_notificados = pedidos_ja_notificados_hoje(chave_transp)
                codigos_novos = [c for c in codigos if c not in ja_notificados]
                if not codigos_novos:
                    logger.info(f"  {chave_transp}: {len(codigos)} pedido(s), todos já notificados hoje.")
                    continue

                nome_exibicao = detalhes[codigos_novos[0]]["nome_transportadora"]
                email_transp = next(
                    (detalhes[c]["email_transportadora"] for c in codigos_novos
                     if detalhes[c]["email_transportadora"]), "")

                itens = []
                for codigo in codigos_novos:
                    page.goto(f"{URL_PROVIDER_SHW}/{_extrair_id(codigo)}",
                             wait_until="networkidle", timeout=30_000)
                    page.wait_for_timeout(400)
                    caminho_xml = baixar_xml_nfe(page, codigo)
                    if not caminho_xml:
                        logger.warning(f"  {codigo}: sem XML de NF-e anexado na Stokki -- pulado.")
                        contadores["sem_xml"] += 1
                        continue
                    sender_id = detalhes[codigo]["sender_id"]
                    embarcador = embarcadores.get(sender_id, {})
                    itens.append({
                        "codigo": codigo,
                        "caminho_xml": caminho_xml,
                        "nf": _extrair_nnf(caminho_xml),
                        "embarcador": embarcador.get("nome", ""),
                        "emails_embarcador": embarcador.get("emails", []),
                    })

                if not itens:
                    continue

                if not email_transp:
                    logger.warning(f"  {nome_exibicao}: sem e-mail cadastrado (planilha nem Stokki) -- "
                                   f"{len(itens)} pedido(s) não notificado(s).")
                    contadores["sem_email"] += 1
                    continue

                cc = sorted({e for i in itens for e in i["emails_embarcador"]})

                assunto = f"[Freshlog] Notas fiscais para entrega — {data_br} — {len(itens)} pedido(s)"
                conteudo = _montar_conteudo(nome_exibicao, data_br, itens)
                corpo = envelope_html(conteudo, rodape="Mensagem automática — Agente Stokki Eventos.")
                anexos = [(i["caminho_xml"], f"{i['codigo']}_NFe.xml") for i in itens]

                destinos = [EMAIL_TESTE] if modo_teste else [email_transp]
                cc_final = [] if modo_teste else cc

                if modo_teste:
                    logger.info(f"  [TESTE] {nome_exibicao} -> {EMAIL_TESTE} "
                               f"(original: {email_transp}, cc original: {cc}) | "
                               f"{len(itens)} pedido(s): {[i['codigo'] for i in itens]}")

                if enviar_email(destinos, assunto, corpo, config_email, cc=cc_final, anexos=anexos):
                    contadores["transportadoras_notificadas"] += 1
                    contadores["pedidos_enviados"] += len(itens)
                    if not modo_teste:
                        for i in itens:
                            registrar_notificacao(i["codigo"], chave_transp)
                else:
                    contadores["falhas"] += 1

            browser.close()

        resumo_etapas["Resumo geral"] = {
            "status": "erro" if contadores["falhas"] else "ok",
            "detalhe": f"{contadores['transportadoras_notificadas']} transportadora(s) notificada(s), "
                      f"{contadores['pedidos_enviados']} pedido(s) enviado(s), "
                      f"{contadores['sem_email']} transportadora(s) sem e-mail, "
                      f"{contadores['sem_xml']} pedido(s) sem XML, "
                      f"{contadores['falhas']} falha(s) de envio.",
        }

    duracao = time.time() - inicio
    logger.info(f"Concluído em {duracao:.1f}s: {resumo_etapas['Resumo geral']['detalhe']}")

    if modo_teste:
        logger.info("[MODO TESTE] Notificação de execução não enviada.")
    else:
        try:
            notificar_execucao(resumo_etapas, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")

    return 1 if contadores["falhas"] else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Notifica transportadoras de redespacho com o XML da NF-e dos pedidos do dia")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Roda a identificação e o download normalmente, mas redireciona todo "
                             "e-mail (sem CC) para hugo@freshlogbr.com e não marca fingerprint")
    parser.add_argument("--data", default="hoje",
                        help="Data alvo: 'hoje' (padrão), 'amanhã' ou DD/MM/AAAA")
    args = parser.parse_args()
    sys.exit(main(modo_teste=args.modo_teste, data_str=args.data))
