# -*- coding: utf-8 -*-
"""
nf_splitter.py

Separa um PDF que junta VÁRIAS DANFEs num arquivo só (ex: o
"NFs FRESH DD.MM.pdf" que o De Tommaso manda por e-mail, com as notas
de todos os pedidos do dia dentro) em um PDF por NF -- mesmo desenho
do boleto_splitter.py, pedido do Hugo, 11/08.

Heurística: toda DANFE começa com o canhoto "RECEBEMOS DE ..." no
topo da primeira página (confirmado nas DANFEs reais do projeto:
NFePHP/Stokki e emissor do De Tommaso/CIAO) -- cada ocorrência desse
texto marca o COMEÇO de uma NF nova. Uma DANFE pode ter mais de 1
página (FOLHA 01/02) -- as páginas de continuação não repetem o
canhoto, então agrupar entre uma ocorrência e a próxima preserva a
nota inteira.

PDF com 0 ou 1 ocorrência (não é DANFE, ou é uma nota única) -- não
separa nada, devolve o arquivo original como está.
"""
import logging
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

MARCADOR_INICIO_NF = "recebemos de"


def separar_nfs(caminho_pdf: Path, pasta_saida: Path) -> list[Path]:
    """
    Retorna uma lista de caminhos -- um PDF por DANFE encontrada
    dentro de caminho_pdf. Se achar só 0 ou 1, devolve [caminho_pdf]
    (o arquivo original, sem separar).
    """
    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            paginas_inicio = [
                i for i, pagina in enumerate(pdf.pages)
                if MARCADOR_INICIO_NF in (pagina.extract_text() or "").lower()
            ]
            total_paginas = len(pdf.pages)
    except Exception as e:
        logger.warning(f"  {caminho_pdf.name}: falha ao ler PDF pra checar DANFEs múltiplas: {e}")
        return [caminho_pdf]

    if len(paginas_inicio) <= 1:
        return [caminho_pdf]

    pasta_saida.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(caminho_pdf))
    resultado = []
    for idx, inicio in enumerate(paginas_inicio):
        fim = paginas_inicio[idx + 1] if idx + 1 < len(paginas_inicio) else total_paginas
        writer = PdfWriter()
        for p in range(inicio, fim):
            writer.add_page(reader.pages[p])
        caminho_saida = pasta_saida / f"{caminho_pdf.stem}_NF{idx + 1}.pdf"
        with open(caminho_saida, "wb") as f:
            writer.write(f)
        resultado.append(caminho_saida)

    logger.info(f"  {caminho_pdf.name}: separado em {len(resultado)} NF(s).")
    return resultado
