# -*- coding: utf-8 -*-
"""
processar_documentos.py

Orquestrador do agente de documentos por pedido -- pedido do Hugo,
05/08 e 10/08: trata Nota Fiscal, Boleto, Carta de Correção,
Agendamento e qualquer outro documento disponível, buscando por
E-MAIL e por STOKKI, classificando, casando com o pedido certo
(PS-XXXXX), e enviando pro Google Cloud Storage.

Fluxo:
  1. Busca PDFs novos por e-mail (busca ampla, últimos N dias)
  2. Busca documentos na Stokki (por enquanto, Fase 1: só o DANFE,
     gerado a partir do XML da NF-e -- ver stokki_documentos.py). Lista
     de pedidos: --pedidos PS-1,PS-2 pra testar manualmente, ou
     auto-descoberta (padrão) seguindo a mesma regra de prioridade da
     subida pro VUUPT -- ver selecionar_pedidos.py.
  3. Pra cada PDF (de qualquer origem): calcula hash, pula se já
     processado, classifica o tipo, casa com um pedido, envia pro GCS
  4. Documento que não casa com nenhum pedido -- fica marcado como
     REVISAO_MANUAL (fingerprint_documentos.py), não trava o resto

COMO USAR:
    py -3.11 processar_documentos.py --modo-teste
    py -3.11 processar_documentos.py --modo-teste --pedidos PS-35471,PS-35472
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
from matcher import casar_documento_com_pedido, extrair_nf_da_danfe, IndexadorNF
from boleto_parser import extrair_metadados_boleto
from fingerprint_documentos import (calcular_hash, ja_processado, marcar_processado,
                                    pedidos_nf_pendentes, atualizar_nf_pedido)
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


def processar_um_documento(item: dict, vuupt, config: dict, modo_teste: bool,
                           tipos_permitidos: set[str] | None = None,
                           indexador_nf: IndexadorNF | None = None) -> str:
    """
    item: {"caminho_local", "nome_arquivo", "assunto_email" (opcional)}
    tipos_permitidos: se informado, documento classificado com um tipo
    fora desse conjunto é ignorado (status "FORA_DE_ESCOPO") -- usado
    pela busca de e-mail de embarcadores conhecidos, que por enquanto
    só trata Boleto (pedido do Hugo, 10/08).
    indexador_nf: índice NF->pedido compartilhado da execução -- as
    DANFEs processadas o alimentam, os boletos consultam (spec de
    boletos parcelados, 11/08).
    Retorna o status final: "JA_PROCESSADO", "ENVIADO", "REVISAO_MANUAL",
    "FORA_DE_ESCOPO", "ERRO".
    """
    caminho = item["caminho_local"]
    nome_arquivo = item["nome_arquivo"]
    assunto_email = item.get("assunto_email")

    hash_conteudo = calcular_hash(caminho)
    if ja_processado(hash_conteudo):
        return "JA_PROCESSADO"

    classificacao = classificar_documento(caminho)
    if tipos_permitidos is not None and classificacao["tipo"] not in tipos_permitidos:
        logger.info(f"  {nome_arquivo}: classificado como '{classificacao['tipo']}', "
                   f"fora do escopo atual ({tipos_permitidos}) -- ignorado.")
        return "FORA_DE_ESCOPO"

    texto_completo = _extrair_texto_pdf_completo(caminho)

    # Metadados específicos por tipo: NF da DANFE alimenta o índice,
    # metadados do boleto habilitam o casamento por NF (regras 2/3)
    numero_nf = cnpj_contraparte = None
    numero_parcela = total_parcelas = None
    metadados_boleto = None
    if classificacao["tipo"] == "Nota Fiscal":
        numero_nf, cnpj_contraparte = extrair_nf_da_danfe(texto_completo)
    elif classificacao["tipo"] == "Boleto":
        metadados_boleto = extrair_metadados_boleto(caminho, texto_pdf=texto_completo)
        numero_nf = metadados_boleto["numero_nf"]
        cnpj_contraparte = metadados_boleto["cnpj_pagador"]
        numero_parcela = metadados_boleto["parcela_atual"]
        total_parcelas = metadados_boleto["total_parcelas"]

    correspondencia = casar_documento_com_pedido(nome_arquivo, assunto_email, texto_completo, vuupt,
                                                 tipo_documento=classificacao["tipo"],
                                                 metadados_boleto=metadados_boleto,
                                                 indexador_nf=indexador_nf)

    codigo_pedido = correspondencia["codigo_pedido"]
    origem = "email" if assunto_email is not None else "stokki"

    if not codigo_pedido:
        logger.warning(f"  {nome_arquivo}: não casou com nenhum pedido -- {correspondencia['motivo_falha']}")
        if not modo_teste:
            marcar_processado(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], None,
                             "REVISAO_MANUAL", motivo=correspondencia["motivo_falha"],
                             numero_nf=numero_nf, numero_parcela=numero_parcela,
                             total_parcelas=total_parcelas, cnpj_contraparte=cnpj_contraparte)
        return "REVISAO_MANUAL"

    info_parcela = (f", parcela {numero_parcela}/{total_parcelas or '?'}"
                    if numero_parcela is not None else "")
    info_nf = f", NF {numero_nf}" if numero_nf else ""
    logger.info(f"  {nome_arquivo}: classificado como '{classificacao['tipo']}' "
               f"(confiança {classificacao['confianca']}), casado com {codigo_pedido} "
               f"(método: {correspondencia['metodo']}{info_nf}{info_parcela})")

    # DANFE casada alimenta o índice NF->pedido da execução atual --
    # os boletos da etapa de e-mail (que roda depois) já enxergam
    if indexador_nf is not None and classificacao["tipo"] == "Nota Fiscal":
        indexador_nf.registrar(numero_nf, cnpj_contraparte, codigo_pedido)

    if modo_teste:
        logger.info(f"  [TESTE] Enviaria pro GCS: {codigo_pedido}/{classificacao['tipo']}/{nome_arquivo}")
        return "ENVIADO"

    try:
        gcs_path = storage_gcs.enviar_documento(config, caminho, codigo_pedido, classificacao["tipo"])
        marcar_processado(hash_conteudo, origem, nome_arquivo, classificacao["tipo"], codigo_pedido,
                         "ENVIADO", gcs_path=gcs_path, numero_nf=numero_nf,
                         numero_parcela=numero_parcela, total_parcelas=total_parcelas,
                         cnpj_contraparte=cnpj_contraparte)
        return "ENVIADO"
    except Exception as e:
        logger.error(f"  {nome_arquivo}: falha ao enviar pro GCS: {e}")
        return "ERRO"


def _backfill_nf_danfes_locais(indexador: IndexadorNF):
    """DANFEs processadas ANTES da migração de 11/08 estão no banco sem
    numero_nf. Os PDFs delas ainda existem em downloads_stokki_temp/ --
    extrai a NF de cada um, atualiza o banco e alimenta o índice. Roda
    rápido (só olha pedidos pendentes que ainda têm o PDF local)."""
    pendentes = pedidos_nf_pendentes()
    if not pendentes:
        return
    from stokki_documentos import PASTA_TEMP_DOWNLOADS
    preenchidos = 0
    for codigo_ps in pendentes:
        caminho = PASTA_TEMP_DOWNLOADS / f"{codigo_ps}_DANFE.pdf"
        if not caminho.exists():
            continue
        numero_nf, cnpj = extrair_nf_da_danfe(_extrair_texto_pdf_completo(caminho))
        if numero_nf:
            atualizar_nf_pedido(codigo_ps, numero_nf, cnpj)
            indexador.registrar(numero_nf, cnpj, codigo_ps)
            preenchidos += 1
    if preenchidos:
        logger.info(f"Backfill de NF: {preenchidos} DANFE(s) antiga(s) indexada(s) a partir dos PDFs locais.")


def main(modo_teste: bool = False, pedidos_stokki: list[str] | None = None):
    inicio = time.time()
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Processamento de documentos iniciado.")

    config = _carregar_config()
    resumo_etapas = {}

    contadores = {"JA_PROCESSADO": 0, "ENVIADO": 0, "REVISAO_MANUAL": 0, "ERRO": 0}

    try:
        vuupt = VuuptClient(config.get("vuupt_api", {}).get("token", ""))

        # Índice NF->pedido: DANFEs do banco + backfill dos PDFs locais.
        # As DANFEs da execução atual entram nele conforme são casadas.
        indexador_nf = IndexadorNF()
        _backfill_nf_danfes_locais(indexador_nf)
        indexador_nf.carregar_do_banco()

        # ── Etapa 1: e-mail ──────────────────────────────────────────────
        itens_email = buscar_pdfs_por_email(config)
        logger.info(f"{len(itens_email)} PDF(s) encontrado(s) por e-mail.")
        for item in itens_email:
            status = processar_um_documento(item, vuupt, config, modo_teste,
                                           indexador_nf=indexador_nf)
            contadores[status] = contadores.get(status, 0) + 1

        resumo_etapas["Documentos (e-mail)"] = {
            "status": "ok",
            "detalhe": f"{len(itens_email)} encontrado(s)",
        }

        # ── Etapa 2: Stokki ────────────────────────────────────────────────
        # Lista de pedidos: se --pedidos foi passado explicitamente, usa ela
        # (útil pra teste manual); senão, descobre sozinho seguindo a mesma
        # regra de prioridade da subida pro VUUPT -- pedido do Hugo, 10/08
        # (ver selecionar_pedidos.py).
        if pedidos_stokki is not None:
            lista_pedidos = pedidos_stokki
        else:
            from selecionar_pedidos import descobrir_pedidos
            lista_pedidos = descobrir_pedidos(config)
            logger.info(f"{len(lista_pedidos)} pedido(s) selecionado(s) automaticamente pra busca na Stokki.")

        if lista_pedidos:
            from playwright.sync_api import sync_playwright
            from stokki_documentos import _login, buscar_documentos_do_pedido

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_context().new_page()
                _login(page, config)

                # Cada pedido em aberto precisa ser visitado toda vez -- um
                # boleto ou outro documento pode ter sido anexado a qualquer
                # momento na aba Documentos (não tem como saber sem olhar).
                # A DANFE em si (que muda de hash a cada geração) tem seu
                # próprio controle pra não regerar à toa -- ver
                # buscar_documentos_do_pedido -> gerar_danfe.
                total_stokki = 0
                for codigo_ps in lista_pedidos:
                    itens_stokki = buscar_documentos_do_pedido(page, config, codigo_ps)
                    total_stokki += len(itens_stokki)
                    for item in itens_stokki:
                        item["assunto_email"] = None  # marca origem como stokki
                        status = processar_um_documento(item, vuupt, config, modo_teste,
                                                       indexador_nf=indexador_nf)
                        contadores[status] = contadores.get(status, 0) + 1

                browser.close()

            resumo_etapas["Documentos (Stokki)"] = {
                "status": "ok",
                "detalhe": f"{total_stokki} encontrado(s) em {len(lista_pedidos)} pedido(s)",
            }
        else:
            logger.info("Nenhum pedido pra buscar documentos na Stokki nesta execução.")
            resumo_etapas["Documentos (Stokki)"] = {"status": "ok", "detalhe": "Nenhum pedido elegível"}

        # ── Etapa 3: e-mail de embarcadores conhecidos (Boleto) ──────────────
        # Diferente da Etapa 1 (busca ampla): remetentes específicos (Dourado,
        # Maria Dolores/NUU), pasta "Todos os e-mails". Por enquanto só trata
        # Boleto -- pedido do Hugo, 10/08 ("só Boleto por enquanto"). Um PDF
        # que junte vários boletos e/ou NFs num arquivo só (ex: "BOLETOS.pdf"
        # da Dourado, "NFs FRESH DD.MM.pdf" do De Tommaso, ou
        # "DANFEs_Boletos_DD-MM.pdf" da Vida Veg -- esse último mistura NF e
        # boleto no mesmo arquivo) é separado em 1 arquivo por documento
        # antes de classificar/casar -- ver documento_splitter.py. Os tipos
        # aproveitados variam por embarcador (item["tipos_permitidos"], ver
        # REMETENTES_EMBARCADORES em email_documentos.py).
        from email_documentos import buscar_pdfs_por_email_embarcadores
        from documento_splitter import separar_documentos_mistos

        PASTA_BOLETOS_SEPARADOS = Path(__file__).parent / "dados" / "boletos_separados"
        PASTA_NFS_SEPARADAS = Path(__file__).parent / "dados" / "nfs_separadas"

        itens_embarcadores = buscar_pdfs_por_email_embarcadores(config)
        logger.info(f"{len(itens_embarcadores)} PDF(s) encontrado(s) de embarcadores conhecidos.")

        total_boletos = 0
        for item in itens_embarcadores:
            partes = separar_documentos_mistos(item["caminho_local"], PASTA_NFS_SEPARADAS, PASTA_BOLETOS_SEPARADOS)
            for caminho_separado, _tipo_detectado in partes:
                sub_item = {**item, "caminho_local": caminho_separado, "nome_arquivo": caminho_separado.name}
                status = processar_um_documento(sub_item, vuupt, config, modo_teste,
                                               tipos_permitidos=item.get("tipos_permitidos") or {"Boleto"},
                                               indexador_nf=indexador_nf)
                if status != "FORA_DE_ESCOPO":
                    contadores[status] = contadores.get(status, 0) + 1
                    total_boletos += 1

        resumo_etapas["Documentos (e-mail embarcadores)"] = {
            "status": "ok",
            "detalhe": f"{total_boletos} documento(s) de {len(itens_embarcadores)} anexo(s)",
        }

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
                        help="Códigos de pedido pra buscar documentos na Stokki, separados por vírgula "
                             "(ex: PS-1,PS-2). Se omitido, descobre sozinho (ver selecionar_pedidos.py).")
    args = parser.parse_args()

    lista_pedidos = [p.strip() for p in args.pedidos.split(",") if p.strip()] if args.pedidos else None
    main(modo_teste=args.modo_teste, pedidos_stokki=lista_pedidos)
