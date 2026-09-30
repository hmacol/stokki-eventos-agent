# -*- coding: utf-8 -*-
"""
documento_splitter.py

Separa um PDF que pode juntar NFs e boletos NUM MESMO ARQUIVO (ex:
"DANFEs_Boletos_DD-MM.pdf" que a Vida Veg manda por e-mail) em um PDF
por documento, cada um no tipo correto -- pedido do Hugo, 11/08.

Generaliza nf_splitter.py e boleto_splitter.py: aqueles dois tratam o
marcador de NF ("recebemos de") e o de boleto ("recibo do pagador")
em SEQUÊNCIA (primeiro tenta separar por NF; se não achar nada,
tenta por boleto) -- o que só funciona se o arquivo for só-NF ou
só-boleto. Num arquivo que mistura os dois (De Tommaso não mistura,
mas a Vida Veg manda o mesmo PDF com DANFEs e boletos intercalados),
esse encadeamento perde os boletos: o nf_splitter agrupa tudo que vem
depois da última NF -- incluindo páginas de boleto -- dentro do
último pedaço de NF, porque não conhece o marcador de boleto.

Esta função olha os DOIS marcadores ao mesmo tempo: cada ocorrência
de QUALQUER um deles marca o início de um documento novo, e as
páginas entre uma ocorrência e a próxima (de qualquer tipo) formam
esse documento -- funciona corretamente com NF e boleto intercalados
em qualquer ordem. Um PDF só-NF ou só-boleto dá o mesmo resultado que
nf_splitter.py/boleto_splitter.py dariam sozinhos.
"""
import logging
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

MARCADOR_INICIO_NF = "recebemos de"
MARCADOR_INICIO_BOLETO = "recibo do pagador"


def separar_documentos_mistos(caminho_pdf: Path, pasta_nfs: Path, pasta_boletos: Path) -> list[tuple[Path, str | None]]:
    """
    Retorna lista de (caminho, tipo) -- um PDF por NF ou boleto
    encontrado dentro de caminho_pdf, "tipo" é "Nota Fiscal" ou
    "Boleto". Se achar 0 ou 1 marcador (de qualquer tipo) no total,
    devolve [(caminho_pdf, None)] (o arquivo original, sem separar --
    tipo desconhecido, fica pro classificador.py decidir, igual
    antes).
    """
    try:
        with pdfplumber.open(caminho_pdf) as pdf:
            marcadores = []
            for i, pagina in enumerate(pdf.pages):
                texto = (pagina.extract_text() or "").lower()
                if MARCADOR_INICIO_NF in texto:
                    marcadores.append((i, "Nota Fiscal"))
                elif MARCADOR_INICIO_BOLETO in texto:
                    marcadores.append((i, "Boleto"))
            total_paginas = len(pdf.pages)
    except Exception as e:
        logger.warning(f"  {caminho_pdf.name}: falha ao ler PDF pra checar documentos múltiplos: {e}")
        return [(caminho_pdf, None)]

    if len(marcadores) <= 1:
        return [(caminho_pdf, None)]

    pasta_nfs.mkdir(parents=True, exist_ok=True)
    pasta_boletos.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(caminho_pdf))
    resultado = []
    contadores = {"Nota Fiscal": 0, "Boleto": 0}
    for idx, (inicio, tipo) in enumerate(marcadores):
        fim = marcadores[idx + 1][0] if idx + 1 < len(marcadores) else total_paginas
        writer = PdfWriter()
        for p in range(inicio, fim):
            writer.add_page(reader.pages[p])

        contadores[tipo] += 1
        if tipo == "Nota Fiscal":
            caminho_saida = pasta_nfs / f"{caminho_pdf.stem}_NF{contadores[tipo]}.pdf"
        else:
            caminho_saida = pasta_boletos / f"{caminho_pdf.stem}_{contadores[tipo]}.pdf"
        with open(caminho_saida, "wb") as f:
            writer.write(f)
        resultado.append((caminho_saida, tipo))

    logger.info(f"  {caminho_pdf.name}: separado em {contadores['Nota Fiscal']} NF(s) e "
               f"{contadores['Boleto']} boleto(s).")
    return resultado
