# -*- coding: utf-8 -*-
"""
matcher.py

Descobre a qual pedido (PS-XXXXX) um documento pertence -- pedido do
Hugo, 05/08: "tenta pelo nome/assunto primeiro, cai pro conteúdo do
PDF se não achar".

Duas etapas, nessa ordem:
  1. Código do pedido (PS-XXXXX) direto no nome do arquivo ou no
     assunto do e-mail -- rápido, não precisa de nenhuma chamada à API.
  2. Se não achou: extrai CNPJ do texto do PDF, busca o CLIENTE por
     esse CNPJ no VUUPT (buscar_customer_por_code -- mesmo campo
     'code' usado em todo o resto do projeto pra CNPJ/CPF), depois
     busca os SERVIÇOS desse cliente. Se achar exatamente 1, casa; se
     achar 0 ou mais de 1, não casa (fica pra revisão manual -- melhor
     não adivinhar errado do que grudar documento no pedido errado).
"""
import re

PADRAO_CODIGO_PEDIDO = re.compile(r"PS-?\d{4,6}", re.IGNORECASE)
PADRAO_CNPJ = re.compile(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}")


def _normalizar_codigo(bruto: str) -> str:
    """'ps12345' ou 'PS-12345' ou 'ps_12345' -> 'PS-12345' (formato
    padrão usado em todo o projeto)."""
    digitos = re.sub(r"\D", "", bruto)
    return f"PS-{digitos}"


def extrair_codigo_pedido(texto: str) -> str | None:
    match = PADRAO_CODIGO_PEDIDO.search(texto or "")
    return _normalizar_codigo(match.group(0)) if match else None


def extrair_cnpj(texto: str) -> str | None:
    """Retorna só os dígitos do primeiro CNPJ encontrado (14 dígitos),
    formato compatível com o campo 'code' dos clientes no VUUPT."""
    match = PADRAO_CNPJ.search(texto or "")
    if not match:
        return None
    digitos = re.sub(r"\D", "", match.group(0))
    return digitos if len(digitos) == 14 else None


def extrair_cnpj_pagador_boleto(texto: str) -> str | None:
    """Pro CNPJ que interessa num BOLETO: o do PAGADOR (cliente
    destinatário, ligado a UM pedido específico) -- não o do
    Beneficiário (o embarcador que emitiu o boleto, sempre o MESMO
    CNPJ em todos os boletos dele, então inútil pra descobrir qual
    pedido é -- lição de pedidos reais da Dourado, 10/08, onde o
    primeiro CNPJ do texto sempre batia com o próprio embarcador).
    Procura o primeiro CNPJ depois da palavra "Pagador"; cai pro
    genérico (primeiro CNPJ do documento) se não achar esse padrão."""
    if texto:
        pos = texto.lower().find("pagador")
        if pos != -1:
            match = PADRAO_CNPJ.search(texto, pos)
            if match:
                digitos = re.sub(r"\D", "", match.group(0))
                if len(digitos) == 14:
                    return digitos
    return extrair_cnpj(texto)


def casar_documento_com_pedido(nome_arquivo: str, assunto_email: str | None,
                               texto_pdf: str, vuupt, tipo_documento: str | None = None) -> dict:
    """
    Retorna {"codigo_pedido": "PS-XXXXX" ou None, "metodo": "...",
    "motivo_falha": "..." (só quando codigo_pedido é None)}.

    tipo_documento: quando é "Boleto", usa extrair_cnpj_pagador_boleto()
    em vez do genérico extrair_cnpj() -- ver docstring de lá.
    """
    codigo = extrair_codigo_pedido(nome_arquivo)
    if codigo:
        return {"codigo_pedido": codigo, "metodo": "nome_arquivo", "motivo_falha": None}

    if assunto_email:
        codigo = extrair_codigo_pedido(assunto_email)
        if codigo:
            return {"codigo_pedido": codigo, "metodo": "assunto_email", "motivo_falha": None}

    cnpj = (extrair_cnpj_pagador_boleto(texto_pdf) if tipo_documento == "Boleto"
            else extrair_cnpj(texto_pdf))
    if not cnpj:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": "Sem código de pedido no nome/assunto, e sem CNPJ reconhecível no PDF."}

    try:
        cliente = vuupt.buscar_customer_por_code(cnpj)
    except Exception as e:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"CNPJ {cnpj} encontrado no PDF, mas falha ao buscar cliente no VUUPT: {e}"}

    if not cliente:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"CNPJ {cnpj} encontrado no PDF, mas nenhum cliente com esse código no VUUPT."}

    try:
        servicos = vuupt.listar_servicos(
            [{"field": "customer_id", "operator": "eq", "value": cliente["id"]}], per_page=50
        )
    except Exception as e:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"Cliente do CNPJ {cnpj} encontrado, mas falha ao buscar serviços dele: {e}"}

    if len(servicos) == 1:
        codigo_encontrado = servicos[0].get("code", "").lstrip("#")
        return {"codigo_pedido": codigo_encontrado, "metodo": "cnpj_pdf", "motivo_falha": None}
    if len(servicos) == 0:
        return {"codigo_pedido": None, "metodo": None,
                "motivo_falha": f"CNPJ {cnpj} casou com um cliente no VUUPT, mas ele não tem nenhum serviço."}
    return {"codigo_pedido": None, "metodo": None,
            "motivo_falha": f"CNPJ {cnpj} casou com um cliente que tem {len(servicos)} serviços -- "
                            f"ambíguo demais pra casar automaticamente."}
