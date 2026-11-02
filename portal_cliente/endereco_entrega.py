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

import json
import logging
import re
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

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
    end = {
        "cep": cep, "logradouro": logradouro, "numero": numero,
        "complemento": str(campos.get("complemento") or "").strip(),
        "bairro": str(campos.get("bairro") or "").strip() or info.get("bairro") or "",
        "municipio": info.get("cidade") or "", "uf": info.get("uf") or "", "cmun": cmun,
    }
    # o que o cliente vê tem de ser o que vai no XML: recusa em vez de cortar
    for campo, nome in (("logradouro", "A rua"), ("numero", "O número"), ("complemento", "O complemento"), ("bairro", "O bairro")):
        if len(end[campo]) > _MAX_TEXTO:
            raise ErroEndereco(f"{nome} pode ter até {_MAX_TEXTO} caracteres")
    return end


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


# ── "Última entrega" do destinatário (spec, seção 3) ──────────────────────────

_ORIGENS_ESCOLHIDAS = ("cliente", "ultima_entrega", "nota")


def _codigo_base(codigo) -> str:
    """'#PS-1-R2' -> 'PS-1': a reentrega é tentativa do mesmo pedido."""
    return re.sub(r"-R\d+$", "", str(codigo or "").replace("#", "").strip().upper())


def _numero_da_linha(linha: str) -> str:
    """Número de 'Rua X, 10 - Compl' (formato de destinatario_endereco)."""
    partes = (linha or "").split(",", 1)
    return partes[1].split(" - ")[0].strip() if len(partes) > 1 else ""


def _norm_numero(numero: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", str(numero or "").upper())


def _catalogo_padrao():
    import envio_pedidos
    from regras.transportadoras import CatalogoTransportadoras
    return CatalogoTransportadoras.carregar_do_banco(envio_pedidos.DB_PATH)


def _entregas_vuupt(conn, sender_id: int, doc: str) -> list:
    try:
        return conn.execute("""
            SELECT r.data_rota, p.endereco, p.codigo
              FROM nucleo_paradas p
              JOIN nucleo_rotas r ON r.id = p.rota_id
              JOIN nucleo_pedidos ped ON ped.codigo = REPLACE(p.codigo, '#', '')
             WHERE p.situacao IN ('ENTREGUE', 'PARCIAL', 'INSUCESSO')
               -- o endereco vem da parada: ela tem de ser do embarcador (codigo
               -- se repete entre embarcadores na Vuupt; revisao de seguranca 30/10)
               AND p.sender_id = ?
               AND (ped.sender_id IS NULL OR ped.sender_id = p.sender_id)
               AND REPLACE(REPLACE(REPLACE(ped.destinatario_codigo, '.', ''), '/', ''), '-', '') = ?
             ORDER BY r.data_rota DESC, p.completed_at DESC
        """, (sender_id, doc)).fetchall()
    except Exception as e:  # banco sem o núcleo: só a memória do portal
        logger.warning(f"ultima_entrega: leitura do nucleo falhou ({e})")
        return []


def ultima_entrega(conn, cnpj_embarcador: str, sender_id: int | None, destinatario_doc: str, catalogo=None) -> dict | None:
    """Endereço que valeu na última entrega do par (embarcador, destinatário):
    tentativa concluída na Vuupt (onde aparece a correção da equipe) ou, pra
    pedido ainda sem tentativa, o que o cliente escolheu no portal. Mais
    recente ganha; no empate, a Vuupt. Nunca atravessa embarcadores."""
    doc = _so_digitos(destinatario_doc)
    candidatos = []
    concluidos = set()
    if sender_id is not None:
        linhas = _entregas_vuupt(conn, sender_id, doc)
        concluidos = {_codigo_base(r[2]) for r in linhas}
        if linhas and catalogo is None:
            try:
                catalogo = _catalogo_padrao()
            except Exception as e:
                logger.warning(f"ultima_entrega: catalogo de transportadoras indisponivel ({e})")
        for data, texto, _codigo in linhas:
            cep = cep_de_texto(texto)
            if not cep or (catalogo is not None and catalogo.resolver_por_endereco(texto)):
                continue  # sem CEP não dá pra comparar; galpão de redespacho não é o destinatário
            candidatos.append({"data": data, "texto": texto, "cep": cep, "numero": numero_de_texto(texto),
                               "campos": None, "fonte": "vuupt"})
            break
    for r in conn.execute("""
            SELECT criado_em, codigo_pedido, endereco_entrega_origem, endereco_entrega_json, destinatario_endereco,
                   destinatario_bairro, destinatario_municipio, destinatario_uf, destinatario_cep
              FROM portal_envios
             WHERE cnpj_embarcador = ? AND destinatario_doc = ? AND status NOT IN ('CANCELADO', 'CANCELANDO')
               AND endereco_entrega_origem IN (?, ?, ?)
             ORDER BY criado_em DESC""", (_so_digitos(cnpj_embarcador), doc, *_ORIGENS_ESCOLHIDAS)):
        if r[1] and _codigo_base(r[1]) in concluidos:
            continue  # já teve tentativa: o que valeu está na Vuupt
        if r[2] == "nota":
            linha = r[4] or ""
            campos = {"cep": _so_digitos(r[8]), "numero": _numero_da_linha(linha),
                      "logradouro": linha.split(",")[0].strip(), "bairro": r[5] or ""}
            nfe = {"destinatario_endereco": linha, "destinatario_bairro": r[5], "destinatario_municipio": r[6],
                   "destinatario_uf": r[7], "destinatario_cep": r[8]}
        else:
            campos = json.loads(r[3] or "{}")
            campos = {k: campos.get(k, "") for k in ("cep", "logradouro", "numero", "complemento", "bairro",
                                                      "municipio", "uf", "cmun")}
            nfe = {"destinatario_endereco": linha_endereco(campos), "destinatario_bairro": campos["bairro"],
                   "destinatario_municipio": campos["municipio"], "destinatario_uf": campos["uf"],
                   "destinatario_cep": campos["cep"]}
        candidatos.append({"data": str(r[0])[:10], "texto": linha_fiscal(nfe), "cep": _so_digitos(campos["cep"]),
                           "numero": campos["numero"], "campos": campos, "fonte": "portal"})
        break
    if not candidatos:
        return None
    ult = max(candidatos, key=lambda c: (c["data"], c["fonte"] == "vuupt"))
    a, m, d = ult["data"].split("-")
    return {**ult, "data_br": f"{d}/{m}"}


def mesmo_endereco(ult: dict, nfe: dict) -> bool:
    """Vuupt: só o CEP (o texto não separa o número com segurança). Portal:
    CEP e número."""
    if ult["cep"] != _so_digitos(nfe.get("destinatario_cep")):
        return False
    if ult["fonte"] == "vuupt":
        return True
    return _norm_numero(ult["numero"]) == _norm_numero(_numero_da_linha(nfe.get("destinatario_endereco") or ""))


def classificar_entrega(conn, nfe: dict, cnpj_embarcador: str, sender_id: int | None, catalogo=None) -> dict:
    """Caso da NF na prévia: nota (já tem <entrega>), terceiros/retirada
    (sem troca), sugestao (última entrega foi em outro endereço) ou livre."""
    caso = {"modo": "livre", "transportadora": "", "sugestao": None}
    if nfe.get("endereco_fiscal"):
        return {**caso, "modo": "nota"}
    import envio_pedidos
    nome = nfe.get("transportadora_nome") or ""
    try:
        tipo = envio_pedidos._tipo_transportadora(nome, nfe.get("transportadora_cnpj") or "")
    except Exception as e:
        logger.warning(f"classificar_entrega: cadastro de transportadoras falhou ({e}); NF {nfe.get('numero_nf')}")
        tipo = None
    if tipo in ("TERCEIROS", "RETIRADA"):
        return {**caso, "modo": "terceiros" if tipo == "TERCEIROS" else "retirada", "transportadora": nome}
    ult = ultima_entrega(conn, cnpj_embarcador, sender_id, nfe.get("destinatario_doc") or "", catalogo=catalogo)
    if ult and not mesmo_endereco(ult, nfe):
        campos = ult["campos"] or {"cep": ult["cep"], "numero": ult["numero"]}
        return {**caso, "modo": "sugestao", "sugestao": {"data_br": ult["data_br"], "texto": ult["texto"], "campos": campos}}
    return caso
