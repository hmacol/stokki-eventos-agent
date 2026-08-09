# -*- coding: utf-8 -*-
"""
processar_documentos.py

Orquestrador do agente de documentos por pedido -- pedido do Hugo,
05/08: trata Nota Fiscal, Boleto, Carta de Correção, Agendamento e
qualquer outro documento disponível, buscando por E-MAIL e por
STOKKI, classificando, casando com o pedido certo (PS-XXXXX), e
enviando pro Google Cloud Storage.

Fluxo:
  1. Busca PDFs novos por e-mail (busca ampla, últimos N dias)
  2. Busca PDFs na aba Documentos da Stokki, pra uma lista de pedidos
     dada (--pedidos PS-1,PS-2,... -- busca por TODOS os pedidos seria
     lento demais; ver notas no código)
  3. Pra cada PDF (de qualquer origem): calcula hash, pula se já
     processado, classifica o tipo, casa com um pedido, envia pro GCS
  4. Documento que não casa com nenhum pedido -- fica marcado como
     REVISAO_MANUAL (fingerprint_documentos.py), não trava o resto

COMO USAR:
    py -3.11 processar_documentos.py --modo-teste
    py -3.11 processar_documentos.py --pedidos PS-35471,PS-35472
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))
sys.path.insert(0, str(Path(__file__).parent))

(Path(__file__).parent / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "dados" / "processar_documentos.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("processar_documentos")

import yaml

from vuupt_client import VuuptClient
from notificar_execucao_agente import notificar_execucao

from classificador import classificar_documento
from matcher import casar_documento_com_pedido
from fingerprint_documentos import calcular_hash, ja_processado, marcar_processado
from email_documentos import buscar_pdfs_por_email
import storage_gcs


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _extrair_texto_pdf_completo(caminho_pdf: Path) -> str:
    """Texto completo do PDF (não só as 2 primeiras páginas do
    classificador) -- usado pro matcher tentar achar CNPJ em qualquer
    lugar do documento."""
    try:
        import pdfplumber
        with pdfplumber.open(caminho_pdf) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return ""


def processar_um_documento(item: dict, vuupt, config: dict, modo_teste: bool) -> str:
    """
    item: {"caminho_local", "nome_arquivo", "assunto_email" (opcional)}
    Retorna o status final: "JA_PROCESSADO", "ENVIADO", "REVISAO_MANUAL", "ERRO".
    """
    caminho = item["caminho_local"]
    nome_arquivo = item["nome_arquivo"]
    assunto_email = item.get("assunto_email")

    hash_conteudo = calcular_hash(caminho)
    if ja_processado(hash_conteudo):
        return "JA_PROCESSADO"

    classificacao = classificar_documento(caminho)
    texto_completo = _extrair_texto_pdf_completo(caminho)
    correspondencia = casar_documento_com_pedido(nome_arquivo, assunto_email, texto_completo, vuupt)

    codigo_pedido = correspondencia["codigo_pedido"]
    origem = "email" if assunto_email is not None else "stokki"

    if not codigo_pedido:
        logger.warning(f"  {nome_arquivo}: não casou com nenhum pedido -- {correspondencia['motivo_falha']}")
        if not modo_teste:
            marcar_processado(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], None,
                             "REVISAO_MANUAL", motivo=correspondencia["motivo_falha"])
        return "REVISAO_MANUAL"

    logger.info(f"  {nome_arquivo}: classificado como '{classificacao['tipo']}' "
               f"(confiança {classificacao['confianca']}), casado com {codigo_pedido} "
               f"(método: {correspondencia['metodo']})")

    if modo_teste:
        logger.info(f"  [TESTE] Enviaria pro GCS: {codigo_pedido}/{classificacao['tipo']}/{nome_arquivo}")
        return "ENVIADO"

    try:
        gcs_path = storage_gcs.enviar_documento(config, caminho, codigo_pedido, classificacao["tipo"])
        marcar_processado(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], codigo_pedido,
                         "ENVIADO", gcs_path=gcs_path)
        return "ENVIADO"
    except Exception as e:
        logger.error(f"  {nome_arquivo}: falha ao enviar pro GCS: {e}")
        return "ERRO"


def main(modo_teste: bool = False, pedidos_stokki: list[str] | None = None):
    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Processamento de documentos iniciado.")

    config = _carregar_config()
    resumo_etapas = {}

    contadores = {"JA_PROCESSADO": 0, "ENVIADO": 0, "REVISAO_MANUAL": 0, "ERRO": 0}

    try:
        vuupt = VuuptClient(config.get("vuupt_api", {}).get("token", ""))

        # ── Etapa 1: e-mail ──────────────────────────────────────────────
        itens_email = buscar_pdfs_por_email(config)
        logger.info(f"{len(itens_email)} PDF(s) encontrado(s) por e-mail.")
        for item in itens_email:
            status = processar_um_documento(item, vuupt, config, modo_teste)
            contadores[status] = contadores.get(status, 0) + 1

        resumo_etapas["Documentos (e-mail)"] = {
            "status": "ok",
            "detalhe": f"{len(itens_email)} encontrado(s)",
        }

        # ── Etapa 2: Stokki (lista explícita de pedidos, ver docstring) ──
        if pedidos_stokki:
            from playwright.sync_api import sync_playwright
            from stokki_documentos import _login, buscar_documentos_do_pedido

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_context().new_page()
                _login(page, config)

                total_stokki = 0
                for codigo_ps in pedidos_stokki:
                    itens_stokki = buscar_documentos_do_pedido(page, config, codigo_ps)
                    total_stokki += len(itens_stokki)
                    for item in itens_stokki:
                        item["assunto_email"] = None  # marca origem como stokki
                        status = processar_um_documento(item, vuupt, config, modo_teste)
                        contadores[status] = contadores.get(status, 0) + 1

                browser.close()

            resumo_etapas["Documentos (Stokki)"] = {
                "status": "ok",
                "detalhe": f"{total_stokki} encontrado(s) em {len(pedidos_stokki)} pedido(s)",
            }
        else:
            logger.info("Nenhum pedido especificado pra busca na Stokki (--pedidos) -- pulando essa etapa.")
            resumo_etapas["Documentos (Stokki)"] = {"status": "ok", "detalhe": "Pulada (nenhum --pedidos informado)"}

        resumo_etapas["Resumo geral"] = {
            "status": "erro" if contadores["ERRO"] else "ok",
            "detalhe": f"{contadores['ENVIADO']} enviado(s), {contadores['REVISAO_MANUAL']} pra revisão manual, "
                      f"{contadores['JA_PROCESSADO']} já processado(s) antes, {contadores['ERRO']} erro(s)",
        }

    except Exception as e:
        logger.exception(f"Erro no processamento de documentos: {e}")
        resumo_etapas["Erro geral"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"Processamento de documentos finalizado em {duracao:.1f}s. Contadores: {contadores}")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Processa documentos PDF (NF, Boleto, CC, Agendamento) por pedido")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Mostra o que seria feito, sem enviar nada pro GCS de verdade")
    parser.add_argument("--pedidos", type=str, default="",
                        help="Códigos de pedido pra buscar documentos na Stokki, separados por vírgula (ex: PS-1,PS-2)")
    args = parser.parse_args()

    lista_pedidos = [p.strip() for p in args.pedidos.split(",") if p.strip()] if args.pedidos else None
    main(modo_teste=args.modo_teste, pedidos_stokki=lista_pedidos)
