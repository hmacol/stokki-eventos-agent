# -*- coding: utf-8 -*-
"""
stokki/trocar_nf.py

Troca o XML da NF-e de um pedido de saida na Stokki pelo mesmo fluxo da tela
("Pedidos de Saida > Opcoes > Informar Nota Fiscal") -- fluxo Cross, Hugo
24/10/2026 (spec docs/superpowers/specs/2026-10-24-importacao-fluxo-cross-design.md):
o pedido nasce com o XML convertido (um item XD) e depois recebe o XML
original, com todos os produtos. Os itens do pedido continuam XD; so o
documento fiscal muda.

Sonda de 24/10 (so leitura, listagem /administrator/inventory/outbound):
  form #form_invoice_standardized -> POST multipart em .../outbound/nfe/store
  com _token, order_id_nfe=<id interno>, action=invoice, page=index,
  nfe=<chave 44 digitos>, file_nfe=<xml>. Erro volta em JSON (errors/message).

Conferencia: a linha da listagem traz o caminho do XML atual do pedido em
btn_danfe data-url. Se o XML baixa por /pt-br/xml/nfe/<base64 do caminho>
(formato de documentos_pedido), confere chave + codigos dos itens; se nao
baixa, confere que o caminho mudou depois do POST. A sonda que confirmaria o
download na area admin nao rodou (leitura em producao negada em 25/10): o
modo usado sai em "conferencia" no resultado e fica pro piloto conferir.

Sem Playwright: usa a StokkiSession (requests). Quem chama segura a trava
stokki/sessao_uso.py (login concorrente derruba a outra sessao).
"""
import base64
import json
import logging
import re
from pathlib import Path

from stokki import pedidos as stokki_pedidos
from stokki.pedidos import BASE_URL

logger = logging.getLogger(__name__)

URL_LISTAGEM = f"{BASE_URL}/pt-br/administrator/inventory/outbound"
URL_NFE_STORE = f"{URL_LISTAGEM}/nfe/store"
URL_XML = f"{BASE_URL}/pt-br/xml/nfe/{{b64}}"

_HEADERS_HTML = {"Accept": "text/html,application/xhtml+xml,*/*", "X-Requested-With": "", "Referer": URL_LISTAGEM}
_RE_CHAVE = re.compile(rb'Id="NFe(\d{44})"')
_RE_DET = re.compile(rb"<(?:\w+:)?det\b")
_RE_CPROD = re.compile(rb"<(?:\w+:)?cProd>([^<]*)</(?:\w+:)?cProd>")
_RE_TOKEN = re.compile(r'<form id="form_invoice_standardized".*?name="_token" value="([^"]+)"', re.S)
_RE_DANFE = re.compile(r'btn_danfe[^>]*data-url="([^"]+)"')


def chave_do_xml(conteudo: bytes) -> str | None:
    m = _RE_CHAVE.search(conteudo or b"")
    return m.group(1).decode() if m else None


def contar_itens(conteudo: bytes) -> int:
    return len(_RE_DET.findall(conteudo or b""))


def codigos_dos_itens(conteudo: bytes) -> list[str]:
    return [c.decode("utf-8", "replace").strip() for c in _RE_CPROD.findall(conteudo or b"")]


def token_csrf(sessao) -> str:
    """_token do form da troca na pagina da listagem; sem ele, o CSRF que a
    StokkiSession guardou no login (o token do Laravel e o mesmo da sessao)."""
    try:
        resp = sessao.get(URL_LISTAGEM, headers=_HEADERS_HTML)
        m = _RE_TOKEN.search(resp.text or "") if getattr(resp, "status_code", 0) == 200 else None
        if m:
            return m.group(1)
    except Exception as e:  # noqa: BLE001 -- cai no token do login
        logger.info(f"trocar_nf: listagem sem token ({e})")
    return getattr(sessao, "_csrf_token", "") or ""


def _texto_linha(linha) -> str:
    valores = linha.values() if isinstance(linha, dict) else linha
    return re.sub(r"<[^>]+>", " ", " ".join(str(v) for v in valores))


def _caminho_da_linha(linha) -> str | None:
    valores = linha.values() if isinstance(linha, dict) else linha
    m = _RE_DANFE.search(" ".join(str(v) for v in valores))
    return m.group(1) if m else None


def _linhas_da_nf(sessao, numero_nf: str, client_id: str) -> list:
    dados = stokki_pedidos.listar_pedidos(sessao, status="", cliente=str(client_id or ""), busca=str(numero_nf),
                                          por_pagina=20, ordenar_coluna="1", ordenar_dir="desc")
    padrao = re.compile(rf"(?<!\d){re.escape(str(numero_nf))}(?!\d)")
    return [l for l in (dados.get("aaData") or []) if padrao.search(_texto_linha(l))]


def localizar_pedido_por_nf(sessao, numero_nf: str, client_id: str) -> dict | None:
    """Pedido mais recente do embarcador com essa NF: {id_stokki, codigo, caminho_nf}."""
    for linha in _linhas_da_nf(sessao, numero_nf, client_id):
        id_stokki = stokki_pedidos.extrair_id_da_linha(linha)
        if id_stokki:
            return {"id_stokki": id_stokki, "codigo": f"PS-{id_stokki}", "caminho_nf": _caminho_da_linha(linha)}
    return None


def caminho_nf_do_pedido(sessao, id_stokki: int, numero_nf: str, client_id: str) -> str | None:
    for linha in _linhas_da_nf(sessao, numero_nf, client_id):
        if stokki_pedidos.extrair_id_da_linha(linha) == int(id_stokki):
            return _caminho_da_linha(linha) or ""
    return None


def baixar_xml_por_caminho(sessao, caminho: str | None) -> bytes | None:
    if not caminho:
        return None
    com_padding = base64.b64encode(caminho.encode("utf-8")).decode()
    # Os links da Stokki parecem vir sem "=" (documentos_pedido repoe o
    # padding ao decodificar); tenta sem e, se nao vier XML, com.
    for token in dict.fromkeys((com_padding.rstrip("="), com_padding)):
        try:
            resp = sessao.get(URL_XML.format(b64=token), headers=_HEADERS_HTML)
        except Exception as e:  # noqa: BLE001 -- sem download, confere pelo caminho
            logger.info(f"trocar_nf: download do XML falhou ({e})")
            continue
        conteudo = getattr(resp, "content", b"") or b""
        if getattr(resp, "status_code", 0) == 200 and b"infNFe" in conteudo:
            return conteudo
    return None


def _erros_da_resposta(resp) -> str:
    try:
        corpo = json.loads(resp.text or "")
    except (ValueError, TypeError):
        corpo = None
    if isinstance(corpo, dict):
        erros = corpo.get("errors")
        if isinstance(erros, dict):
            erros = [m for v in erros.values() for m in (v if isinstance(v, list) else [v])]
        if erros:
            return "; ".join(str(e) for e in (erros if isinstance(erros, list) else [erros]))
        if corpo.get("success") is False or resp.status_code != 200:
            return str(corpo.get("message") or corpo)[:300]
        return ""
    return "" if resp.status_code == 200 else (resp.text or "")[:300]


def trocar_nf(sessao, id_stokki: int, caminho_xml_original: Path, numero_nf: str, client_id: str) -> dict:
    """Devolve {"ok", "ja_estava", "erro", "conferencia" ("itens"|"caminho"|""),
    "itens_original", "itens_pedido", "caminho_antes", "caminho_depois"}.
    Nunca levanta por resposta da Stokki (so por falha de rede do requests)."""
    original = Path(caminho_xml_original).read_bytes()
    chave = chave_do_xml(original)
    codigos = codigos_dos_itens(original)
    r = {"ok": False, "ja_estava": False, "erro": "", "conferencia": "", "itens_original": len(codigos),
         "itens_pedido": None, "caminho_antes": None, "caminho_depois": None}
    if not chave:
        r["erro"] = "XML original sem chave de acesso"
        return r

    def _bate(conteudo: bytes) -> bool:
        return chave_do_xml(conteudo) == chave and codigos_dos_itens(conteudo) == codigos

    antes = caminho_nf_do_pedido(sessao, id_stokki, numero_nf, client_id)
    if antes is None:
        r["erro"] = f"pedido {id_stokki} nao encontrado na listagem da Stokki (NF {numero_nf})"
        return r
    r["caminho_antes"] = antes
    atual = baixar_xml_por_caminho(sessao, antes)
    if atual is not None and _bate(atual):
        r.update(ok=True, ja_estava=True, conferencia="itens", itens_pedido=contar_itens(atual))
        return r

    token = token_csrf(sessao)
    if not token:
        r["erro"] = "token CSRF nao encontrado na pagina de pedidos"
        return r
    resp = sessao.post(
        URL_NFE_STORE,
        files={"_token": (None, token), "order_id_nfe": (None, str(id_stokki)), "action": (None, "invoice"),
               "page": (None, "index"), "nfe": (None, chave), "file_nfe": (f"{chave}.xml", original, "text/xml")},
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": URL_LISTAGEM,
                 "Accept": "application/json, text/javascript, */*; q=0.01"},
    )
    erro = _erros_da_resposta(resp)
    if resp.status_code != 200 or erro:
        r["erro"] = f"Stokki {resp.status_code}: {erro or 'sem detalhe'}"
        return r

    depois = caminho_nf_do_pedido(sessao, id_stokki, numero_nf, client_id)
    r["caminho_depois"] = depois
    baixado = baixar_xml_por_caminho(sessao, depois)
    if baixado is not None:
        r["conferencia"], r["itens_pedido"] = "itens", contar_itens(baixado)
        if _bate(baixado):
            r["ok"] = True
        else:
            r["erro"] = (f"Stokki aceitou mas o XML do pedido nao bate com o original "
                         f"({r['itens_pedido']} item(ns) no pedido, {len(codigos)} no original)")
    else:
        r["conferencia"] = "caminho"
        if depois and depois != antes:
            r["ok"] = True
        else:
            r["erro"] = "Stokki aceitou mas o caminho da NF do pedido nao mudou"
    logger.info(f"Stokki: troca de NF do pedido {id_stokki} (NF {numero_nf}) -> "
                f"{'ok' if r['ok'] else 'FALHOU'} (conferencia por {r['conferencia']})")
    return r
