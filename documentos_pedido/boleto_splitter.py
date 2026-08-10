# -*- coding: utf-8 -*-
"""
boleto_splitter.py

Separa um PDF que junta VÁRIOS boletos num arquivo só (ex: o
"BOLETOS.pdf" que a Laticínios Dourado manda por e-mail a cada lote
de entrega, com o boleto de vários pedidos diferentes dentro) em um
PDF por boleto -- pedido do Hugo, 10/08: "separar por página e casar
cada um".

Heurística: todo boleto bancário brasileiro tem o bloco "RECIBO DO
PAGADOR" logo no início (confirmado nos boletos reais testados,
Itaú) -- cada ocorrência desse texto no topo de uma página marca o
COMEÇO de um boleto novo. Um boleto pode ocupar mais de 1 página
(recibo + ficha de compensação) -- por isso NÃO dá pra simplesmente
cortar por página; agrupa as páginas entre uma ocorrência e a
próxima.

PDF com 0 ou 1 ocorrência de "RECIBO DO PAGADOR" (não é boleto, ou é
um boleto único que já vem sozinho) -- não separa nada, devolve o
arquivo original como está. Mais seguro que inventar um corte errado
num formato que a gente ainda não viu (banco diferente, por exemplo).
"""
import logging
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

MARCADOR_INICIO_BOLETO = "recibo do pagador"


def separar_boletos(caminho_pdf: Path, pasta_saida: Path) -> list[Path]:
    """
    Retorna uma lista de caminhos -- um PDF por boleto encontrado
    dentro de caminho_pdf. Se achar só 0 ou 1 boleto, devolve
    [caminho_pdf] (o arquivo original, sem separar).
    """
    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            paginas_inicio = [
                i for i, pagina in enumerate(pdf.pages)
                if MARCADOR_INICIO_BOLETO in (pagina.extract_text() or "").lower()
            ]
            total_paginas = len(pdf.pages)
    except Exception as e:
        logger.warning(f"  {caminho_pdf.name}: falha ao ler PDF pra checar boletos múltiplos: {e}")
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
        caminho_saida = pasta_saida / f"{caminho_pdf.stem}_{idx + 1}.pdf"
        with open(caminho_saida, "wb") as f:
            writer.write(f)
        resultado.append(caminho_saida)

    logger.info(f"  {caminho_pdf.name}: separado em {len(resultado)} boleto(s).")
    return resultado
