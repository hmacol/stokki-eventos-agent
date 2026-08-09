# -*- coding: utf-8 -*-
"""
regras/complexidade_entrega.py

Nível de complexidade de entrega (1 a 4) por CNPJ/CPF do DESTINATÁRIO,
usado junto com o tipo de carga do embarcador (ver _buscar_dados_
embarcador_banco em pipeline.py, coluna `habilidade` da tabela `interno`)
para montar o nome da skill do VUUPT: "{TipoCarga}-{Nível}".

Substitui regras/classificacao_clientes.py (28/07) — aquele módulo
tratava tipo de carga e nível como uma coisa só vinda de uma planilha
por CNPJ/CPF do destinatário; Hugo corrigiu: o tipo de carga é do
EMBARCADOR (já disponível em interno.habilidade), só o nível de
complexidade é mesmo do destinatário. Arquivo antigo pode ser apagado.

Fonte: planilha Excel mantida pelo Hugo, com pelo menos estas colunas
(nomes tolerantes a variação de acento/espaço/caixa):
    CNPJ/CPF   -- documento do destinatário (com ou sem máscara)
    Nível      -- 1 (mais fácil) a 4 (possível mais de 3h de espera)

Destinatário cujo CNPJ/CPF não está na planilha: aplica NIVEL_PADRAO (1).
"""
import logging
import re
import unicodedata
from pathlib import Path

import openpyxl

logger = logging.getLogger(__name__)

# Aplicado quando o CNPJ/CPF do destinatário não está na planilha —
# nunca fica sem nível nenhum (mesmo espírito do padrão "Seco-1" original,
# aqui só a metade do nível).
NIVEL_PADRAO = 1

NIVEIS_VALIDOS = {1, 2, 3, 4}

# Nomes de coluna aceitos, já normalizados (maiúsculas, sem acento, sem
# espaço/pontuação) — tolera variações comuns de como a planilha pode vir.
# Inclui os nomes reais da planilha BD_CLIENTES do Hugo (Destinatário -
# Código / Classificação Dificuldade) além de variações mais genéricas.
COLUNAS_DOCUMENTO = {
    "CNPJCPF", "CPFCNPJ", "DOCUMENTO", "CNPJ", "CPF",
    "DESTINATARIOCODIGO", "CODIGO",
}
COLUNAS_NIVEL = {
    "NIVEL", "NIVELDECOMPLEXIDADE", "NIVELCOMPLEXIDADE", "COMPLEXIDADE",
    "CLASSIFICACAODIFICULDADE", "DIFICULDADE",
}
# CEP é opcional — só usado para desempatar duplicatas (linha completa vs
# linha incompleta de exportações antigas). Ausência não é erro.
COLUNAS_CEP = {"CEP", "DESTINATARIOCEP"}


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _normalizar_cabecalho(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _normalizar_nivel(v) -> int | None:
    """Aceita int, float (2.0) ou string ('2', '2.0') — None se inválido."""
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None
    return n if n in NIVEIS_VALIDOS else None


def carregar_niveis(caminho: str | Path) -> dict[str, int]:
    """
    Lê a planilha de complexidade e retorna {documento_só_dígitos: nível}.

    Colunas detectadas por nome (tolerante a variação de acento/espaço/
    caixa). Linhas com documento ou nível inválido/vazio são ignoradas
    (contadas e avisadas no log), sem travar a carga do restante.

    Duplicatas de documento (confirmado na planilha real do Hugo — mesmo
    CNPJ aparece várias vezes, geralmente com linhas incompletas de
    exportações antigas misturadas com a linha completa atual):
      - Se todas as ocorrências têm o MESMO nível → usa direto, sem drama.
      - Se divergem: prioriza a(s) linha(s) com CEP preenchido (linha
        incompleta sem endereço é o resíduo, não a fonte confiável).
      - Se ainda divergir mesmo entre linhas com CEP (conflito genuíno —
        confirmado que existe, inclusive com o MESMO CEP e nível
        diferente, ou seja, não dá pra desempatar por endereço) → usa o
        MAIOR nível (mais conservador: melhor alocar tempo a mais do que
        a menos numa entrega difícil) e avisa no log para revisão manual.

    Caminho vazio ou arquivo inexistente: retorna {} (todo destinatário
    cai no NIVEL_PADRAO) — não é erro fatal, só um aviso no log.
    """
    if not caminho:
        return {}

    caminho = Path(caminho)
    if not caminho.exists():
        logger.warning(
            f"Planilha de complexidade de entrega não encontrada: {caminho} — "
            f"todos os destinatários usarão o nível padrão ({NIVEL_PADRAO})."
        )
        return {}

    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    ws = wb.active

    linha_cabecalho = next(ws.iter_rows(min_row=1, max_row=1))
    cabecalho = [_normalizar_cabecalho(c.value) for c in linha_cabecalho]

    def _achar_coluna(candidatos):
        for i, c in enumerate(cabecalho):
            if c in candidatos:
                return i
        return None

    idx_doc = _achar_coluna(COLUNAS_DOCUMENTO)
    idx_niv = _achar_coluna(COLUNAS_NIVEL)
    idx_cep = _achar_coluna(COLUNAS_CEP)

    faltantes = [
        nome for nome, idx in [("CNPJ/CPF", idx_doc), ("Nível", idx_niv)]
        if idx is None
    ]
    if faltantes:
        raise ValueError(
            f"Planilha de complexidade ({caminho}) sem a(s) coluna(s): "
            f"{', '.join(faltantes)}. Cabeçalho encontrado: "
            f"{[c.value for c in linha_cabecalho]}"
        )

    # Coleta todas as ocorrências por documento primeiro (doc -> [(cep, nivel), ...])
    ocorrencias: dict[str, list[tuple[str, int]]] = {}
    ignoradas = 0
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if linha is None or all(v is None for v in linha):
            continue  # linha totalmente vazia (comum no fim de planilhas)

        doc   = _so_digitos(linha[idx_doc])
        nivel = _normalizar_nivel(linha[idx_niv])
        cep   = _so_digitos(linha[idx_cep]) if idx_cep is not None else ""

        if not doc or nivel is None:
            ignoradas += 1
            continue

        ocorrencias.setdefault(doc, []).append((cep, nivel))

    # Resolve cada documento para um único nível, tratando duplicatas
    mapa: dict[str, int] = {}
    conflitos_resolvidos = []
    for doc, ocs in ocorrencias.items():
        niveis_distintos = {n for _, n in ocs}
        if len(niveis_distintos) == 1:
            mapa[doc] = ocs[0][1]
            continue

        # Diverge: prioriza linhas com CEP preenchido
        com_cep = [(c, n) for c, n in ocs if c]
        niveis_com_cep = {n for _, n in com_cep}
        if len(niveis_com_cep) == 1:
            mapa[doc] = com_cep[0][1]
            continue

        # Conflito genuíno (mesmo entre linhas com CEP, ou nenhuma tem CEP)
        # -> mais conservador: usa o maior nível
        candidatos = niveis_com_cep if niveis_com_cep else niveis_distintos
        maior = max(candidatos)
        mapa[doc] = maior
        conflitos_resolvidos.append((doc, sorted(niveis_distintos), maior))

    if ignoradas:
        logger.warning(
            f"{ignoradas} linha(s) da planilha de complexidade ignorada(s) "
            f"(documento ou nível ausente/inválido)."
        )
    if conflitos_resolvidos:
        logger.warning(
            f"{len(conflitos_resolvidos)} documento(s) com nível conflitante entre "
            f"linhas duplicadas — resolvido pelo maior valor (mais conservador). "
            f"Primeiros exemplos: {conflitos_resolvidos[:5]}"
        )
    logger.info(f"Complexidade de entrega carregada: {len(mapa)} documento(s) únicos "
               f"({len(ocorrencias)} documento(s) na planilha, "
               f"{sum(len(v) for v in ocorrencias.values())} linha(s) válida(s) no total).")
    return mapa


def classificar_nivel(documento: str, mapa: dict[str, int]) -> tuple[int, bool]:
    """
    Retorna (nível, encontrado_na_planilha).

    Se o documento vier vazio ou não estiver no mapa, aplica NIVEL_PADRAO
    e retorna encontrado_na_planilha=False.
    """
    doc = _so_digitos(documento)
    if doc and doc in mapa:
        return mapa[doc], True
    return NIVEL_PADRAO, False
