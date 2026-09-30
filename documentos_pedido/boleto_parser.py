# -*- coding: utf-8 -*-
"""
boleto_parser.py

Extrai metadados estruturados de um boleto bancário em PDF -- pedido
do Hugo, 11/08 (spec de boletos parcelados): número da NF, parcela,
valor, vencimento, CNPJ do pagador, linha digitável. Esses metadados
alimentam o casamento boleto->pedido via índice de DANFEs (ver
matcher.py::IndexadorNF).

Regexes calibradas contra os 3 formatos REAIS já vistos em produção
(não só os teóricos da spec):

  1. Dourado / Santander (033-7): "Número do Documento" = o número da
     NF direto, sem sufixo (ex: "06/08/2026 149636 DM N ...").
  2. Maria Dolores-NUU / ERP Olist / Itaú (341-7): "Histórico: Ref. a
     NF nº 40162, ..." (fonte mais explícita que existe) + "No
     documento" no formato "1040162/01" (parcela no sufixo -- o
     prefixo NÃO é a NF pura, por isso o Histórico tem prioridade).
  3. Itaueira / Itaú (341-7): "Número do Documento" = "8482-1/1"
     (NF-parcela/total).

ARMADILHA REAL (lição de 10/08): nos boletos do ERP Olist, o primeiro
CNPJ depois da palavra "Pagador" é o do BENEFICIÁRIO (o cabeçalho
"Recibo do Pagador" vem antes do bloco do beneficiário) -- por isso
extrair_cnpj_pagador() abaixo descarta candidatos iguais ao CNPJ do
beneficiário (o primeiro CNPJ do documento) antes de aceitar.
"""
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Guardas (?<!\d)/(?!\d): sem elas, o padrão casa 14 dígitos DENTRO da
# linha digitável (47 dígitos corridos, como o ERP Olist imprime) e o
# "primeiro CNPJ do documento" vira lixo -- visto em boleto real.
PADRAO_CNPJ = re.compile(r"(?<!\d)\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}(?!\d)")

# "Ref. a NF nº 40162" (ERP Olist) -- fonte explícita, prioridade máxima
PADRAO_NF_HISTORICO = re.compile(r"Ref\.?\s*a\s*NF\s*n?[ºo°.]*\s*(\d{1,9})", re.IGNORECASE)

# Linha de valores da tabela do boleto: "27/07/2026 8482-1/1 DM NÃO ..."
# ou "06/08/2026 149636 DM N ..." ou "31/07/2026 1040162/01 DM N ..."
# -- data do documento, número do documento (com sufixo opcional de
# parcela), espécie DM/DS. O sufixo aparece como "-1/1" (nf-parcela/
# total) ou "/01" (doc/parcela, sem total).
PADRAO_DOC_LINHA = re.compile(
    r"\b\d{2}/\d{2}/\d{4}\s+(\d{3,9})(?:[-/](\d{1,2})(?:/(\d{1,2}))?)?\s+D[MS]\b"
)

# 4º formato real (De Tommaso/CIAO, Itaú 341-7, visto em 11/08): o
# layout quebra a linha antes da data, então a linha de valores vem
# como "documento 035880 DM N Processamento ..." -- o rótulo
# "documento" cola direto no número, sem data na frente.
PADRAO_DOC_SEM_DATA = re.compile(
    r"\bdocumento\s+(\d{3,9})(?:[-/](\d{1,2})(?:/(\d{1,2}))?)?\s+D[MS]\b", re.IGNORECASE
)

# Fallbacks genéricos da spec, quando a linha de valores não bate
PADRAO_NF_GENERICO = [
    re.compile(r"NF\s*[:\.]?\s*(\d{1,9})", re.IGNORECASE),
    re.compile(r"Seu\s+N[úu]mero\s*:\s*(\d{1,9})", re.IGNORECASE),
    re.compile(r"Doc(?:umento)?\s*:\s*(\d{1,9})", re.IGNORECASE),
]
PADRAO_PARCELA_GENERICO = re.compile(r"Parc(?:ela)?\s*[:\.]?\s*(\d{1,2})(?:\s*/\s*(\d{1,2}))?",
                                     re.IGNORECASE)

# "(=) Valor do Documento" / "Valor documento" seguido do valor. A
# janela precisa tolerar dígitos no meio ("101 R$ 1.219,82", "109 REAL
# 1.540,00" -- carteira/moeda vêm antes do valor na mesma linha).
# Aceita com e sem separador de milhar ("1.219,82" e "2966,70").
PADRAO_VALOR = re.compile(
    r"Valor\s+(?:do\s+)?documento.{0,80}?((?:\d{1,3}(?:\.\d{3})*|\d+),\d{2})",
    re.IGNORECASE | re.DOTALL,
)

PADRAO_VENCIMENTO = re.compile(
    r"Vencimento[^\d]{0,200}?(\d{2}/\d{2}/\d{4})", re.IGNORECASE | re.DOTALL
)

# Linha digitável: formatada com pontos/espaços (47 dígitos no total)
# ou colada num bloco só de 47 dígitos (ERP Olist imprime assim)
PADRAO_LINHA_DIGITAVEL = re.compile(
    r"\d{5}\.\d{5}\s+\d{5}\.\d{6}\s+\d{5}\.\d{6}\s+\d\s+\d{14}|\b\d{47}\b"
)

PADRAO_CODIGO_PEDIDO = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)


def _so_digitos(cnpj: str) -> str:
    return re.sub(r"\D", "", cnpj)


def _normalizar_nf(bruto: str) -> str | None:
    digitos = bruto.lstrip("0")
    return digitos or None


def extrair_cnpj_pagador(texto: str) -> str | None:
    """CNPJ do PAGADOR (destinatário da NF, quem identifica o pedido).
    Percorre cada ocorrência de "pagador" e aceita o primeiro CNPJ
    que venha depois E seja diferente do CNPJ do beneficiário (o
    primeiro CNPJ do documento) -- ver armadilha do Olist na docstring
    do módulo."""
    if not texto:
        return None

    primeiro = PADRAO_CNPJ.search(texto)
    cnpj_beneficiario = _so_digitos(primeiro.group(0)) if primeiro else None

    texto_baixo = texto.lower()
    pos = texto_baixo.find("pagador")
    while pos != -1:
        match = PADRAO_CNPJ.search(texto, pos)
        if match:
            digitos = _so_digitos(match.group(0))
            if len(digitos) == 14 and digitos != cnpj_beneficiario:
                return digitos
        pos = texto_baixo.find("pagador", pos + 1)

    return None


def extrair_metadados_boleto(caminho_pdf: Path, texto_pdf: str | None = None) -> dict:
    """
    Retorna:
      {"cnpj_pagador", "numero_nf", "parcela_atual", "total_parcelas",
       "valor", "vencimento" (DD/MM/YYYY como impresso), "codigo_pedido_no_texto",
       "linha_digitavel"}
    Campos que não deram pra extrair vêm como None -- nunca levanta
    exceção por formato desconhecido (o casamento cai pros fallbacks).
    """
    if texto_pdf is None:
        try:
            import pdfplumber
            with pdfplumber.open(caminho_pdf) as pdf:
                texto_pdf = "\n".join(p.extract_text() or "" for p in pdf.pages)
        except Exception as e:
            logger.warning(f"  {caminho_pdf.name}: falha ao extrair texto pro parser de boleto: {e}")
            texto_pdf = ""

    resultado = {
        "cnpj_pagador": extrair_cnpj_pagador(texto_pdf),
        "numero_nf": None,
        "parcela_atual": None,
        "total_parcelas": None,
        "valor": None,
        "vencimento": None,
        "codigo_pedido_no_texto": None,
        "linha_digitavel": None,
    }
    if not texto_pdf:
        return resultado

    # Número da NF + parcela -- na ordem de confiabilidade real
    m_hist = PADRAO_NF_HISTORICO.search(texto_pdf)
    m_doc = PADRAO_DOC_LINHA.search(texto_pdf) or PADRAO_DOC_SEM_DATA.search(texto_pdf)
    if m_doc:
        if m_doc.group(2):
            resultado["parcela_atual"] = int(m_doc.group(2))
        if m_doc.group(3):
            resultado["total_parcelas"] = int(m_doc.group(3))
    if m_hist:
        # Fonte explícita ("Ref. a NF nº X") ganha do número do documento,
        # que no Olist tem prefixo extra (ex: doc 1040162 pra NF 40162)
        resultado["numero_nf"] = _normalizar_nf(m_hist.group(1))
    elif m_doc:
        resultado["numero_nf"] = _normalizar_nf(m_doc.group(1))
    else:
        for padrao in PADRAO_NF_GENERICO:
            m = padrao.search(texto_pdf)
            if m:
                resultado["numero_nf"] = _normalizar_nf(m.group(1))
                break

    if resultado["parcela_atual"] is None:
        m_parc = PADRAO_PARCELA_GENERICO.search(texto_pdf)
        if m_parc:
            resultado["parcela_atual"] = int(m_parc.group(1))
            if m_parc.group(2):
                resultado["total_parcelas"] = int(m_parc.group(2))

    m_valor = PADRAO_VALOR.search(texto_pdf)
    if m_valor:
        try:
            resultado["valor"] = float(m_valor.group(1).replace(".", "").replace(",", "."))
        except ValueError:
            pass

    m_venc = PADRAO_VENCIMENTO.search(texto_pdf)
    if m_venc:
        resultado["vencimento"] = m_venc.group(1)

    m_ps = PADRAO_CODIGO_PEDIDO.search(texto_pdf)
    if m_ps:
        digitos = re.sub(r"\D", "", m_ps.group(0))
        resultado["codigo_pedido_no_texto"] = f"PS-{digitos}"

    m_linha = PADRAO_LINHA_DIGITAVEL.search(texto_pdf)
    if m_linha:
        resultado["linha_digitavel"] = m_linha.group(0)

    return resultado
