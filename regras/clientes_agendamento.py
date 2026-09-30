# -*- coding: utf-8 -*-
"""
regras/clientes_agendamento.py

Cadastro de quais DESTINATÁRIOS (por CNPJ/CPF) exigem agendamento real
de entrega — usado para só preencher scheduled_start/scheduled_end no
VUUPT quando existe de fato uma exigência de agendamento (pedido do
Hugo, 29/07).

Fonte: a MESMA planilha de complexidade de entrega (BD_CLIENTES —
config.yaml complexidade_entrega.planilha / clientes_agendamento.planilha,
apontando pro mesmo arquivo), coluna "ALERTAS CLIENTES" (coluna O).

Essa coluna NÃO é um flag limpo — é um campo de alerta de texto livre
com vários tipos de conteúdo (confirmado com dado real, 29/07):
    'ENTREGA PRIORITÁRIA', 'REDE TAPI', 'AGENDAMENTO', 'AGENDADA', ...
Só uma fração dos clientes tem QUALQUER alerta (2 de 5773 tinham algo
relacionado a agendamento no arquivo de teste). Por isso a detecção é
por PALAVRA-CHAVE ("contém AGENDA", normalizado — sem acento/caixa),
não por valor exato — cobre 'AGENDAMENTO', 'AGENDADA', 'AGENDAR' etc.
sem precisar listar cada grafia.

Cliente cujo CNPJ/CPF não tem alerta de agendamento: NÃO tem
agendamento — scheduled_start/scheduled_end ficam de FORA do payload.

Duplicatas de CNPJ na planilha (confirmado ao carregar complexidade):
se QUALQUER linha do mesmo CNPJ tiver o alerta, o cliente é marcado
como tem_agendamento=True (OR entre as ocorrências — mais seguro do
que arriscar perder um agendamento real por causa de uma linha
duplicada/incompleta).
"""
import logging
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

import openpyxl

logger = logging.getLogger(__name__)

COLUNAS_DOCUMENTO = {
    "CNPJCPF", "CPFCNPJ", "DOCUMENTO", "CNPJ", "CPF",
    "DESTINATARIOCODIGO", "CODIGO",
}
COLUNAS_ALERTA = {
    "ALERTASCLIENTES", "ALERTACLIENTE", "ALERTAS", "ALERTA",
}

# Palavra-chave normalizada (sem acento/caixa) que identifica um alerta
# de agendamento entre os vários tipos possíveis na coluna.
PALAVRA_CHAVE_AGENDAMENTO = "AGENDA"

# O que marcar_agendamento escreve na coluna de alertas.
MARCA_AGENDAMENTO = "AGENDA"
COLUNAS_NOME = {"DESTINATARIONOME", "NOME"}


def _so_digitos(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _formas_documento(doc: str) -> set[str]:
    """A planilha guarda o código como NÚMERO, então documento que começa
    com zero chega sem ele (04972092003148 vira 4972092003148) e nunca
    casava com o CNPJ completo do pedido (29/09). Devolve o documento como
    veio mais as formas com os zeros de volta (CPF = 11, CNPJ = 14)."""
    formas = {doc, doc.zfill(14)}
    if len(doc) <= 11:
        formas.add(doc.zfill(11))
    return formas


def _normalizar_cabecalho(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def _normalizar_texto(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).upper().strip()


def carregar_clientes_agendamento(caminho: str | Path) -> set[str]:
    """
    Lê a planilha (a mesma de complexidade de entrega) e retorna o
    conjunto de documentos (só dígitos) dos destinatários cujo campo
    "ALERTAS CLIENTES" contém uma palavra relacionada a agendamento.

    Caminho vazio ou arquivo inexistente: retorna set() — nenhum
    cliente tem agendamento (comportamento seguro por padrão).
    Coluna de alerta ausente: também retorna set() com aviso (mais
    seguro do que travar o pipeline por uma coluna opcional ausente —
    diferente da coluna de documento, que é obrigatória).
    """
    if not caminho:
        return set()

    caminho = Path(caminho)
    if not caminho.exists():
        logger.warning(
            f"Planilha de clientes com agendamento não encontrada: {caminho} — "
            f"nenhum pedido terá agendamento preenchido."
        )
        return set()

    wb = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    ws = wb.active

    linha_cabecalho = next(ws.iter_rows(min_row=1, max_row=1))
    cabecalho = [_normalizar_cabecalho(c.value) for c in linha_cabecalho]

    idx_doc = None
    for i, c in enumerate(cabecalho):
        if c in COLUNAS_DOCUMENTO:
            idx_doc = i
            break
    if idx_doc is None:
        raise ValueError(
            f"Planilha de clientes com agendamento ({caminho}) sem coluna de "
            f"CNPJ/CPF. Cabeçalho encontrado: {[c.value for c in linha_cabecalho]}"
        )

    idx_alerta = None
    for i, c in enumerate(cabecalho):
        if c in COLUNAS_ALERTA:
            idx_alerta = i
            break
    if idx_alerta is None:
        logger.warning(
            f"Planilha de clientes com agendamento ({caminho}) sem coluna de "
            f"alertas (ex: 'ALERTAS CLIENTES') — nenhum pedido terá agendamento "
            f"preenchido. Cabeçalho encontrado: {[c.value for c in linha_cabecalho]}"
        )
        return set()

    documentos: set[str] = set()
    linhas_com_alerta_nao_agendamento = 0
    for linha in ws.iter_rows(min_row=2, values_only=True):
        if linha is None or all(v is None for v in linha):
            continue
        doc = _so_digitos(linha[idx_doc])
        alerta = _normalizar_texto(linha[idx_alerta])
        if not doc or not alerta:
            continue
        if PALAVRA_CHAVE_AGENDAMENTO in alerta:
            documentos.update(_formas_documento(doc))
        else:
            linhas_com_alerta_nao_agendamento += 1

    if linhas_com_alerta_nao_agendamento:
        logger.info(
            f"{linhas_com_alerta_nao_agendamento} linha(s) com outro tipo de alerta "
            f"(não relacionado a agendamento) — ignoradas para este cadastro."
        )
    logger.info(f"Clientes com agendamento carregados: {len(documentos)} documento(s).")
    return documentos


def tem_agendamento(documento: str, conjunto: set[str]) -> bool:
    """True se o CNPJ/CPF do destinatário exige agendamento real de entrega."""
    doc = _so_digitos(documento)
    return bool(doc) and doc in conjunto


def marcar_agendamento(caminho: str | Path, clientes: dict[str, str], gravar: bool = True) -> dict:
    """
    Marca AGENDA na coluna "ALERTAS CLIENTES" dos destinatários de
    `clientes` ({documento: nome}) que ainda não têm alerta de
    agendamento (pedido do Hugo, 29/09: quem já teve pedido com data de
    agendamento informada passa a exigir agendamento sempre).

    Documento que já está na planilha: marca TODAS as linhas dele,
    mantendo o alerta que já existir ("ENTREGA PRIORITÁRIA / AGENDA").
    Documento que não está: linha nova só com código, nome e AGENDA.
    Quem já tem alerta de agendamento em qualquer linha não muda.

    Só grava quando há o que marcar, e antes copia a planilha pra
    BD_CLIENTES_backup_<carimbo>_pre_agenda.xlsx na mesma pasta.
    gravar=False só devolve quem seria marcado, sem tocar no arquivo.

    Retorna {"marcados": [...], "incluidos": [...], "backup": caminho|None}.
    """
    caminho = Path(caminho)
    wb = openpyxl.load_workbook(caminho)
    ws = wb.active

    cabecalho = [_normalizar_cabecalho(c.value) for c in ws[1]]

    def _achar_coluna(candidatos):
        for i, c in enumerate(cabecalho):
            if c in candidatos:
                return i + 1
        return None

    col_doc = _achar_coluna(COLUNAS_DOCUMENTO)
    col_alerta = _achar_coluna(COLUNAS_ALERTA)
    col_nome = _achar_coluna(COLUNAS_NOME)
    if col_doc is None or col_alerta is None:
        raise ValueError(
            f"Planilha de clientes ({caminho}) sem coluna de CNPJ/CPF ou de alertas. "
            f"Cabeçalho encontrado: {[c.value for c in ws[1]]}"
        )

    # sem os zeros à esquerda, que a planilha perde (ver _formas_documento)
    procurados = {_so_digitos(doc).lstrip("0"): _so_digitos(doc) for doc in clientes if _so_digitos(doc)}
    linhas_por_doc: dict[str, list[int]] = {}
    ultima_linha = 1
    for n in range(2, ws.max_row + 1):
        chave = _so_digitos(ws.cell(row=n, column=col_doc).value).lstrip("0")
        if not chave:
            continue
        ultima_linha = n
        if chave in procurados:
            linhas_por_doc.setdefault(chave, []).append(n)

    marcados, incluidos = [], []
    for chave, doc in procurados.items():
        linhas = linhas_por_doc.get(chave, [])
        alertas = [ws.cell(row=n, column=col_alerta).value for n in linhas]
        if any(PALAVRA_CHAVE_AGENDAMENTO in _normalizar_texto(a) for a in alertas):
            continue
        if not linhas:
            ultima_linha += 1
            ws.cell(row=ultima_linha, column=col_doc, value=int(doc))
            if col_nome is not None:
                ws.cell(row=ultima_linha, column=col_nome, value=clientes.get(doc) or None)
            ws.cell(row=ultima_linha, column=col_alerta, value=MARCA_AGENDAMENTO)
            incluidos.append(doc)
            continue
        for n, alerta in zip(linhas, alertas):
            atual = str(alerta or "").strip()
            ws.cell(row=n, column=col_alerta,
                    value=f"{atual} / {MARCA_AGENDAMENTO}" if atual else MARCA_AGENDAMENTO)
        marcados.append(doc)

    if not gravar or (not marcados and not incluidos):
        wb.close()
        return {"marcados": marcados, "incluidos": incluidos, "backup": None}

    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = caminho.with_name(f"{caminho.stem}_backup_{carimbo}_pre_agenda{caminho.suffix}")
    shutil.copy2(caminho, backup)

    # grava num arquivo ao lado e troca no fim: quem estiver lendo a
    # planilha (pipeline, painel, portal) nunca pega um arquivo pela metade
    temporario = caminho.with_name(f"{caminho.stem}.gravando{caminho.suffix}")
    wb.save(temporario)
    wb.close()
    os.replace(temporario, caminho)

    logger.info(f"AGENDA gravado na planilha: {len(marcados)} marcado(s), {len(incluidos)} "
                f"incluído(s). Backup: {backup.name}")
    return {"marcados": marcados, "incluidos": incluidos, "backup": str(backup)}
