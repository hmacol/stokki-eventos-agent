# -*- coding: utf-8 -*-
"""
Endereço de entrega escolhido no envio do portal (spec
docs/superpowers/specs/2026-10-27-endereco-entrega-portal-design.md).

Na prévia do envio por XML o cliente vê pra onde o pedido vai e pode trocar
o endereço de entrega; o escolhido vale pra área, dia fixo e gravação, e
entra como grupo <entrega> no XML que o worker manda pra Stokki (o arquivo
original do cliente nunca muda).

Desligado por padrão: portal_cliente.envios.endereco_entrega_ativo.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

# NF-e: xNome, xLgr, nro, xCpl e xBairro têm no máximo 60 caracteres
_MAX_TEXTO = 60


class ErroEndereco(ValueError):
    """Mensagem pronta pro cliente."""


def _so_digitos(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def entrega_ativa(config: dict | None) -> bool:
    envios = (((config or {}).get("portal_cliente") or {}).get("envios") or {})
    return bool(envios.get("endereco_entrega_ativo", False))


def montar_endereco(campos: dict, consultar=None) -> dict:
    """Valida o que veio do navegador ({cep, numero, complemento?,
    logradouro?, bairro?}) pela consulta do CEP. Município, UF e código IBGE
    vêm sempre da consulta; rua e bairro do cliente quando ele mandou (CEP
    geral de cidade não traz rua)."""
    if consultar is None:
        from cotacao import consultar_cep as consultar
    cep = _so_digitos(campos.get("cep"))
    info = consultar(cep) if len(cep) == 8 else None
    if not info:
        raise ErroEndereco("CEP não encontrado")
    cmun = _so_digitos(info.get("ibge"))
    if len(cmun) != 7:
        raise ErroEndereco("CEP sem código de município: tente de novo em instantes")
    logradouro = str(campos.get("logradouro") or "").strip() or info.get("logradouro") or ""
    if not logradouro:
        raise ErroEndereco("Informe a rua")
    numero = str(campos.get("numero") or "").strip()
    if not numero:
        raise ErroEndereco("Informe o número")
    return {
        "cep": cep, "logradouro": logradouro, "numero": numero,
        "complemento": str(campos.get("complemento") or "").strip(),
        "bairro": str(campos.get("bairro") or "").strip() or info.get("bairro") or "",
        "municipio": info.get("cidade") or "", "uf": info.get("uf") or "", "cmun": cmun,
    }


def linha_endereco(end: dict) -> str:
    """'Rua X, 10 - Compl', o formato de destinatario_endereco do ler_nfe."""
    linha = ", ".join(p for p in (end.get("logradouro"), end.get("numero")) if p)
    compl = end.get("complemento")
    if compl:
        linha = f"{linha} - {compl}" if linha else compl
    return linha


def linha_fiscal(nfe: dict) -> str:
    """Endereço do nfe numa linha só, como fica em endereco_fiscal."""
    cep = _so_digitos(nfe.get("destinatario_cep"))
    cep_txt = f"CEP {cep[:5]}-{cep[5:]}" if len(cep) == 8 else cep
    cidade = f"{nfe.get('destinatario_municipio') or ''}/{nfe.get('destinatario_uf') or ''}".strip("/")
    return ", ".join(p for p in (nfe.get("destinatario_endereco"), nfe.get("destinatario_bairro"), cidade, cep_txt) if p)


def aplicar_entrega(nfe: dict, end: dict) -> dict:
    """Cópia do nfe com o endereço de entrega no lugar do fiscal (que fica em
    endereco_fiscal pra tela). Telefone e destinatário não mudam."""
    novo = dict(nfe)
    novo.update({
        "destinatario_endereco": linha_endereco(end), "destinatario_bairro": end.get("bairro") or "",
        "destinatario_municipio": end.get("municipio") or "", "destinatario_uf": end.get("uf") or "",
        "destinatario_cep": _so_digitos(end.get("cep")), "endereco_fiscal": linha_fiscal(nfe),
    })
    return novo


_FECHA_RETIRADA = re.compile(rb"</(\w+:)?retirada>")
_FECHA_DEST = re.compile(rb"</(\w+:)?dest>")
_TEM_ENTREGA = re.compile(rb"<(\w+:)?entrega[\s>/]")


def inserir_entrega_xml(conteudo: bytes, end: dict, documento: str, nome: str) -> bytes:
    """Insere o grupo <entrega> por texto (não reescreve namespaces nem o
    resto do documento), na posição do layout: depois de </retirada> se
    houver, senão depois de </dest>."""
    if _TEM_ENTREGA.search(conteudo):
        raise ValueError("o XML já tem o grupo <entrega>")
    achado = _FECHA_RETIRADA.search(conteudo) or _FECHA_DEST.search(conteudo)
    if not achado:
        raise ValueError("XML sem o grupo <dest>")
    pre = (achado.group(1) or b"").decode()
    doc = _so_digitos(documento)

    def tag(nome_tag: str, valor, limite: int | None = _MAX_TEXTO) -> str:
        texto = str(valor or "").strip()
        if limite:
            texto = texto[:limite]
        return f"<{pre}{nome_tag}>{escape(texto, {chr(34): '&quot;'})}</{pre}{nome_tag}>"

    partes = [tag("CPF" if len(doc) == 11 else "CNPJ", doc, None), tag("xNome", nome),
              tag("xLgr", end.get("logradouro")), tag("nro", end.get("numero"))]
    if end.get("complemento"):
        partes.append(tag("xCpl", end["complemento"]))
    partes += [tag("xBairro", end.get("bairro")), tag("cMun", end.get("cmun"), None),
               tag("xMun", end.get("municipio")), tag("UF", end.get("uf"), None),
               tag("CEP", _so_digitos(end.get("cep")), None)]
    bloco = f"<{pre}entrega>{''.join(partes)}</{pre}entrega>".encode("utf-8")
    return conteudo[:achado.end()] + bloco + conteudo[achado.end():]


_CEP_TEXTO = re.compile(r"\b(\d{5})-?(\d{3})\b")
_NUMERO = re.compile(r"^(\d+[A-Za-z]?|S/?N)$", re.I)


def cep_de_texto(texto: str) -> str:
    """CEP (8 dígitos) do endereço da Vuupt ('..., Cidade - UF, 00000-000, Brasil')."""
    achados = _CEP_TEXTO.findall(texto or "")
    return "".join(achados[-1]) if achados else ""


def numero_de_texto(texto: str) -> str:
    """Número do imóvel: 2ª parte do endereço da Vuupt, se for um número."""
    partes = [p.strip() for p in (texto or "").split(",")]
    return partes[1] if len(partes) > 1 and _NUMERO.match(partes[1]) else ""
