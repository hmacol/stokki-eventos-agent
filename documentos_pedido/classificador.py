# -*- coding: utf-8 -*-
"""
classificador.py

Classifica um PDF em um dos tipos: Nota Fiscal, Boleto, Carta de
Correção, Agendamento, ou Outro -- pedido do Hugo, 05/08.

Duas fontes de sinal, combinadas (nome do arquivo é mais confiável
quando bate, texto do PDF é o fallback/reforço):
  1. Palavras-chave no NOME do arquivo (mais rápido, não precisa abrir
     o PDF)
  2. Palavras-chave no TEXTO do PDF (via pdfplumber -- mesma
     biblioteca usada no agente de documentos anterior)

IMPORTANTE (lição do agente anterior): Carta de Correção tem que ser
checada ANTES de Nota Fiscal -- o texto de uma CC menciona "nota
fiscal" o tempo todo (é uma correção DE uma nota fiscal), então
checar NF primeiro classificaria toda CC como NF por engano.
"""
import re
from pathlib import Path

# Ordem importa -- do mais específico pro mais genérico. CC antes de NF
# (ver docstring). Cada tipo: (nome_display, padrões de nome de
# arquivo, padrões de texto do PDF).
TIPOS_DOCUMENTO = [
    ("Carta de Correção", [r"carta.*corre[cç][aã]o", r"\bcc-?e\b", r"correcao"],
     [r"carta de corre[cç][aã]o", r"cc-?e\s*n[uú]mero", r"corre[cç][aã]o de nota fiscal"]),
    ("Boleto", [r"boleto", r"\bbolet[oa]\b"],
     [r"linha digit[aá]vel", r"vencimento", r"c[oó]digo de barras", r"boleto banc[aá]rio"]),
    ("Agendamento", [r"agendamento", r"\bagenda\b"],
     [r"data de agendamento", r"hor[aá]rio de entrega agendad", r"confirma[cç][aã]o de agendamento"]),
    ("Nota Fiscal", [r"\bnf-?e?\b", r"nota.*fiscal", r"\bdanfe\b"],
     [r"danfe", r"nota fiscal eletr[oô]nica", r"chave de acesso"]),
]


def _texto_do_pdf(caminho_pdf: Path, max_paginas: int = 2) -> str:
    """Extrai texto das primeiras páginas do PDF (o suficiente pra
    classificar -- não precisa ler o documento inteiro). Retorna
    string vazia se falhar (PDF corrompido, escaneado sem OCR, etc)
    -- nesse caso a classificação cai só no nome do arquivo."""
    try:
        import pdfplumber
        with pdfplumber.open(caminho_pdf) as pdf:
            paginas = pdf.pages[:max_paginas]
            return "\n".join(p.extract_text() or "" for p in paginas)
    except Exception:
        return ""


def classificar_documento(caminho_pdf: Path) -> dict:
    """
    Retorna {"tipo": nome_display ou "Outro", "confianca": "alta"/"media"/"baixa",
    "sinal": "nome_arquivo"/"texto_pdf"/"nenhum"}.

    "alta": bateu tanto no nome do arquivo quanto no texto.
    "media": bateu só num dos dois.
    "baixa"/"Outro": não bateu em nada -- não é NF/Boleto/CC/Agendamento
    reconhecível, mas ainda é salvo (categoria "Outro", pedido do
    Hugo: "qualquer outro documento disponível").
    """
    nome_normalizado = caminho_pdf.name.lower()
    texto = _texto_do_pdf(caminho_pdf).lower()

    sinais = [
        (tipo_nome,
         any(re.search(p, nome_normalizado) for p in padroes_nome),
         any(re.search(p, texto) for p in padroes_texto) if texto else False)
        for tipo_nome, padroes_nome, padroes_texto in TIPOS_DOCUMENTO
    ]

    # Nome do arquivo é o sinal mais confiável (ver docstring) -- varre
    # TODOS os tipos por nome (respeitando a ordem específico->genérico
    # de TIPOS_DOCUMENTO) antes de cair pro texto. Sem isso, um tipo
    # mais genérico da lista podia vencer só por bater no texto antes
    # de um tipo mais específico, com nome inequívoco, ser considerado.
    for tipo_nome, bate_nome, bate_texto in sinais:
        if bate_nome and bate_texto:
            return {"tipo": tipo_nome, "confianca": "alta", "sinal": "nome_arquivo+texto_pdf"}
    for tipo_nome, bate_nome, _ in sinais:
        if bate_nome:
            return {"tipo": tipo_nome, "confianca": "media", "sinal": "nome_arquivo"}
    for tipo_nome, _, bate_texto in sinais:
        if bate_texto:
            return {"tipo": tipo_nome, "confianca": "media", "sinal": "texto_pdf"}

    return {"tipo": "Outro", "confianca": "baixa", "sinal": "nenhum"}
