# -*- coding: utf-8 -*-
"""
stokki/produtos.py

Catálogo de produtos do Stokki (Cadastro → Produtos), lido pela mesma
sessão de cookies dos pedidos. Descoberto em 04/09/2026:

  GET /pt-br/administrator/client/product/table   — DataTables (formato
      legado: aaData / iTotalRecords), params draw/start/length + client,
      state (""|Active|Inactive|...), input_search. ~1.800 linhas.
      A célula `sku` é HTML com nome, cliente, SKU, GTIN/EAN e categoria;
      `checkbox` traz o id do produto; `updated` a última atualização.
  GET /pt-br/administrator/client/product/show/{id} — perfil completo
      (unidade, "Lote: Sim/Não", GTIN/EAN, DUN 1 + Quantidade 1 da caixa,
      pesos). É uma página HTML: rótulo numa linha, valor na seguinte.

  /client/product/excel responde 405 (não é GET) — não usado.
"""
import logging
import re
import time
from datetime import datetime

from bs4 import BeautifulSoup

from stokki.auth import BASE_URL

logger = logging.getLogger(__name__)

URL_TABELA = f"{BASE_URL}/pt-br/administrator/client/product/table"
URL_PAGINA = f"{BASE_URL}/pt-br/administrator/client/product"
URL_PERFIL = f"{BASE_URL}/pt-br/administrator/client/product/show/{{id}}"

_HEADERS_AJAX = {
    "Referer": URL_PAGINA,
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "application/json, text/javascript, */*; q=0.01",
}
_HEADERS_HTML = {"Referer": URL_PAGINA, "X-Requested-With": "", "Accept": "text/html,*/*"}


def _texto(html: str) -> str:
    return " ".join(BeautifulSoup(str(html or ""), "html.parser").get_text(" ", strip=True).split())


def _data_br_para_iso(texto: str) -> str | None:
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\D*(\d{2}:\d{2})?", texto or "")
    if not m:
        return None
    d, mo, y, hm = m.groups()
    return f"{y}-{mo}-{d} {hm or '00:00'}:00"


def _parsear_linha(row: dict) -> dict | None:
    cb = BeautifulSoup(row.get("checkbox", ""), "html.parser").find("input")
    if not cb or not cb.get("value"):
        return None
    stokki_id = int(cb["value"])
    sku_soup = BeautifulSoup(row.get("sku", ""), "html.parser")
    strong = sku_soup.find("strong")
    descricao = " ".join(strong.get_text(" ", strip=True).split()) if strong else ""
    campos = {}
    for span in sku_soup.select("span"):
        small = span.find("small", class_="text-muted", recursive=False)
        if not small:
            continue
        rotulo = small.get_text(strip=True).upper()
        copia = span.find("a", class_="btn_copy_text")
        if copia and copia.get("data-text"):
            valor = copia["data-text"].strip()
        else:
            interno = span.find("span", attrs={"title": True})
            if interno:
                valor = interno["title"].strip()
            else:
                valor = " ".join(small.next_sibling.split()) if isinstance(small.next_sibling, str) else ""
                valor = valor.replace("\xa0", " ").strip()
        campos[rotulo] = valor
    # ícone "possui lote": presente sempre, invisível (#ffffff00) quando não tem
    controla_lote = 0
    for a in sku_soup.find_all("a", attrs={"title": True}):
        if "possui lote" in a["title"].lower() and "ffffff00" not in (a.get("style") or "").replace(" ", "").lower():
            controla_lote = 1
    estado = _texto(row.get("state", ""))
    return {
        "stokki_id": stokki_id,
        "descricao": descricao or campos.get("SKU") or f"Produto #{stokki_id}",
        "embarcador": campos.get("CLIENTE") or None,
        "sku": campos.get("SKU") or None,
        "ean": campos.get("GTIN/EAN") or None,
        "categoria": campos.get("CATEGORIA") or None,
        "controla_lote": controla_lote,
        "ativo": 1 if estado.lower().startswith("ativo") else 0,
        "estado_stokki": estado,
        "origem_stokki": _texto(row.get("origin", "")),
        "stokki_atualizado_em": _data_br_para_iso(_texto(row.get("updated", ""))),
    }


def listar_produtos(sess, tamanho_pagina: int = 500, estado: str = "", pausa: float = 0.2) -> list[dict]:
    """Percorre a tabela inteira e devolve uma lista de dicts (ver _parsear_linha)."""
    produtos, inicio, total = [], 0, None
    draw = 1
    while True:
        params = {
            "draw": draw, "start": inicio, "length": tamanho_pagina, "search[value]": "",
            "order[0][column]": 2, "order[0][dir]": "asc",
            "client": "", "state": estado, "input_search": "",
        }
        # StokkiSession já fixa timeout=30 por dentro (passar de novo dá TypeError).
        resp = sess.get(URL_TABELA, params=params, headers=_HEADERS_AJAX)
        resp.raise_for_status()
        j = resp.json()
        linhas = j.get("aaData") or j.get("data") or []
        total = int(j.get("iTotalDisplayRecords") or j.get("recordsFiltered") or j.get("iTotalRecords") or 0)
        for row in linhas:
            p = _parsear_linha(row)
            if p:
                produtos.append(p)
        logger.info("Produtos Stokki: página start=%d -> %d linhas (total %d)", inicio, len(linhas), total)
        inicio += len(linhas)
        draw += 1
        if not linhas or inicio >= total:
            break
        time.sleep(pausa)
    return produtos


_ROTULOS_PERFIL = {
    "Unidade de Medida": "unidade",
    "Quantidade por Volume": "qtd_por_volume",
    "Lote": "lote",
    "GTIN/EAN": "ean",
    "DUN 1": "dun",
    "Quantidade 1": "qtd_por_caixa",
    "Tipo 1": "tipo_caixa",
    "Peso Líquido": "peso_liquido",
    "Peso Bruto": "peso_bruto",
    "NCM": "ncm",
}


def _numero(texto: str | None) -> float | None:
    if not texto:
        return None
    m = re.search(r"-?\d+(?:[.,]\d+)?", texto.replace(".", "").replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def ler_perfil(sess, stokki_id: int) -> dict:
    """Lê a página show/{id} e devolve os campos úteis pro WMS. A página é
    rótulo numa linha e valor na próxima; 'Não informado' vira None."""
    resp = sess.get(URL_PERFIL.format(id=int(stokki_id)), headers=_HEADERS_HTML)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    corpo = soup.find("section", class_="content") or soup.body or soup
    linhas = [l.strip() for l in corpo.get_text("\n").split("\n") if l.strip()]
    bruto = {}
    for i, l in enumerate(linhas[:-1]):
        chave = _ROTULOS_PERFIL.get(l)
        if chave and chave not in bruto:
            valor = linhas[i + 1]
            bruto[chave] = None if valor.lower().startswith("não informado") else valor
    embarcador_id = None
    m = re.search(r"#stkc-(\d+)", resp.text)
    if m:
        embarcador_id = int(m.group(1))
    perfil = {
        "unidade": (bruto.get("unidade") or "UN").upper()[:6],
        "ean": re.sub(r"\D", "", bruto.get("ean") or "") or None,
        "dun": re.sub(r"\D", "", bruto.get("dun") or "") or None,
        "qtd_por_caixa": _numero(bruto.get("qtd_por_caixa")) or _numero(bruto.get("qtd_por_volume")),
        "peso_liquido_kg": _numero(bruto.get("peso_liquido")),
        "controla_lote": 1 if (bruto.get("lote") or "").strip().lower().startswith("sim") else 0,
        "embarcador_id": embarcador_id,
        "ncm": bruto.get("ncm"),
        "lido_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    return perfil