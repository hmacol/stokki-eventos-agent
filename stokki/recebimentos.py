# -*- coding: utf-8 -*-
"""
stokki/recebimentos.py

Leitura de recebimentos (incoming/inbound) via endpoints internos do
Stokki -- mesmo estilo de stokki/pedidos.py (outbound).

Parametros levantados ao vivo na producao pelo Hugo em 22/09/2026 (NAO
sondar de novo desta maquina -- login concorrente derruba a sessao da
VPS e do agente-importacao-stokki):

Endpoints:
  GET /pt-br/administrator/inventory/incoming/table       -- listagem paginada (DataTables)
  GET /pt-br/administrator/inventory/incoming/show/{id}   -- detalhe completo

Colunas do DataTable (nesta ordem, indices 0-11):
  checkbox, id, arrival_date, type, motion, client, origin, destination,
  receipt_date, marker, state, action

Filtros que a tela envia: state, marker, client, unit, input_search.
client=48 (Maria Dolores) confirmado -- filtra do lado do servidor.

ARMADILHA -- a linha tem dois numeros diferentes no campo 'id':
    <a href="https://freshlog.stokki.com.br/.../incoming/show/2478">#PE-2478</a><br>
    <span class="text-muted">41099</span>
O codigo e '#PE-2478', o id que abre o detalhe e 2478 (do href). O 41099
e outro id interno que NAO serve -- incoming/show/41099 devolve 500.
Extrair sempre do href (ou do '#PE-<n>'), nunca do numero solto.

Estados vistos: "Recebido" e "Em transito". Um recebimento "Em transito"
ainda nao tem a tabela de itens (mercadoria nao chegou). Um "Recebido"
sem a tabela tambem foi visto (achado do Hugo, amostra de 8 recebimentos
da Maria Dolores) -- o parser tem que aguentar os dois casos sem quebrar,
devolvendo lista vazia.

Tabelas do detalhe de um recebimento:
  - "NR. | ID | SKU | Nome | Localizacao | Quantidade | unidade | Valor Unitario"
  - "Produto | Lote | Entrada | Fabricacao | Validade | Quantidade" <- e esta
    que importa: lote e validade JA vem preenchidos na Stokki (61 de 61
    itens numa amostra de 8 recebimentos). extrair_itens_do_recebimento
    busca essa tabela pelo cabecalho (robusto a mudanca de layout, mesma
    tecnica de stokki.pedidos.extrair_itens_do_pedido), NUNCA a outra.

Decisao do Hugo (22/09/2026): lote e validade lidos aqui sao SUGESTAO --
o operador confirma ou corrige na tela do celular (tarefa seguinte). Por
isso o item extraido carrega "lote" e "validade" alem das chaves que
extrair_itens_do_pedido ja usa (sku, ean_linha, descricao, qtd_embalagem,
linha). Este endpoint nao tem coluna EAN -- ean_linha sai sempre vazio;
quem resolve o produto (wms_pedidos.resolver_item) cai na regra por SKU.
"""
import logging
import re
import time as _time

from bs4 import BeautifulSoup

from stokki.auth import StokkiSession

logger = logging.getLogger(__name__)

BASE_URL = "https://freshlog.stokki.com.br"

RE_SKU_DESCRICAO = re.compile(r"^\s*(\S+)\s+-\s+(.*)$")


# ── Listagem paginada ─────────────────────────────────────────────────────────

def listar_recebimentos(
    sessao: StokkiSession,
    status: str = "all",
    cliente: str = "",
    marcador: str = "",
    unidade: str = "",
    busca: str = "",
    pagina: int = 0,
    por_pagina: int = 100,
    ordenar_coluna: str = "1",
    ordenar_dir: str = "desc",
) -> dict:
    """
    Retorna uma pagina de recebimentos no formato DataTables (aaData /
    iTotalRecords), mesmo formato do outbound/table.

    Parametros exatos que a tela envia -- mesmo cuidado de
    stokki.pedidos.listar_pedidos: a Stokki devolve 500 quando o conjunto
    de colunas esta incompleto.
    """
    params = {
        "draw": 1,
        "_": int(_time.time() * 1000),
        "columns[0][data]": "checkbox",     "columns[0][name]": "", "columns[0][searchable]": "true",  "columns[0][orderable]": "false", "columns[0][search][value]": "", "columns[0][search][regex]": "false",
        "columns[1][data]": "id",           "columns[1][name]": "", "columns[1][searchable]": "true",  "columns[1][orderable]": "true",  "columns[1][search][value]": "", "columns[1][search][regex]": "false",
        "columns[2][data]": "arrival_date", "columns[2][name]": "", "columns[2][searchable]": "true",  "columns[2][orderable]": "true",  "columns[2][search][value]": "", "columns[2][search][regex]": "false",
        "columns[3][data]": "type",         "columns[3][name]": "", "columns[3][searchable]": "true",  "columns[3][orderable]": "false", "columns[3][search][value]": "", "columns[3][search][regex]": "false",
        "columns[4][data]": "motion",       "columns[4][name]": "", "columns[4][searchable]": "true",  "columns[4][orderable]": "false", "columns[4][search][value]": "", "columns[4][search][regex]": "false",
        "columns[5][data]": "client",       "columns[5][name]": "", "columns[5][searchable]": "true",  "columns[5][orderable]": "false", "columns[5][search][value]": "", "columns[5][search][regex]": "false",
        "columns[6][data]": "origin",       "columns[6][name]": "", "columns[6][searchable]": "true",  "columns[6][orderable]": "false", "columns[6][search][value]": "", "columns[6][search][regex]": "false",
        "columns[7][data]": "destination",  "columns[7][name]": "", "columns[7][searchable]": "true",  "columns[7][orderable]": "false", "columns[7][search][value]": "", "columns[7][search][regex]": "false",
        "columns[8][data]": "receipt_date", "columns[8][name]": "", "columns[8][searchable]": "true",  "columns[8][orderable]": "false", "columns[8][search][value]": "", "columns[8][search][regex]": "false",
        "columns[9][data]": "marker",       "columns[9][name]": "", "columns[9][searchable]": "true",  "columns[9][orderable]": "false", "columns[9][search][value]": "", "columns[9][search][regex]": "false",
        "columns[10][data]": "state",       "columns[10][name]": "", "columns[10][searchable]": "true", "columns[10][orderable]": "false", "columns[10][search][value]": "", "columns[10][search][regex]": "false",
        "columns[11][data]": "action",      "columns[11][name]": "", "columns[11][searchable]": "true", "columns[11][orderable]": "false", "columns[11][search][value]": "", "columns[11][search][regex]": "false",
        "order[0][column]": ordenar_coluna,
        "order[0][dir]": ordenar_dir,
        "search[value]": "",
        "search[regex]": "false",
        "start": pagina * por_pagina,
        "length": por_pagina,
        # filtros especificos do Stokki -- exatamente os que a tela envia
        "state": status,
        "marker": marcador,
        "client": cliente,
        "unit": unidade,
        "input_search": busca,
    }
    resp = sessao.get(
        f"{BASE_URL}/pt-br/administrator/inventory/incoming/table",
        params=params,
        headers={"Referer": f"{BASE_URL}/pt-br/administrator/inventory/incoming"},
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        import json
        return json.loads(resp.text)


# ── Linha da listagem ──────────────────────────────────────────────────────────

def extrair_id_da_linha(linha) -> int | None:
    """
    Extrai o id do Stokki (o que abre /show/{id}) do campo 'id' da linha.
    ARMADILHA: a linha tambem tem outro numero solto (nao-clicavel) que
    NAO serve -- por isso usa sempre o href, nunca o primeiro numero que
    aparecer no texto.
    """
    html = str(linha.get("id", "")) if isinstance(linha, dict) else ""
    m = re.search(r"/show/(\d+)", html)
    return int(m.group(1)) if m else None


def extrair_codigo_da_linha(linha) -> str:
    """Extrai o codigo '#PE-<n>' do campo 'id' da linha (fonte primaria);
    se nao achar o texto, monta a partir do id do href."""
    html = str(linha.get("id", "")) if isinstance(linha, dict) else ""
    m = re.search(r"#PE-\d+", html)
    if m:
        return m.group(0)
    id_stokki = extrair_id_da_linha(linha)
    return f"#PE-{id_stokki}" if id_stokki else ""


def extrair_cabecalho_da_linha(linha) -> dict:
    """
    Extrai os campos de topo de uma linha do aaData de incoming/table:
    id_stokki, codigo, embarcador, situacao, chegada.
    """
    def _texto(campo):
        return re.sub(r"<[^>]+>", " ", str(linha.get(campo, "") if isinstance(linha, dict) else "")).strip()

    return {
        "id_stokki": extrair_id_da_linha(linha),
        "codigo": extrair_codigo_da_linha(linha),
        "embarcador": _texto("client"),
        "situacao": _texto("state"),
        "chegada": _texto("arrival_date"),
    }


# ── Detalhe de um recebimento ──────────────────────────────────────────────────

def extrair_itens_do_recebimento(html: str) -> list[dict]:
    """
    Itens da tabela "Produto | Lote | Entrada | Fabricacao | Validade |
    Quantidade" do detalhe de um recebimento -- e a que importa (lote e
    validade JA vem preenchidos na Stokki). Busca pelo cabecalho, igual
    stokki.pedidos.extrair_itens_do_pedido, pra nao confundir com a outra
    tabela do detalhe (NR/ID/SKU/Nome/Localizacao/.../Valor Unitario).

    Devolve lista vazia se a tabela nao existir (recebimento "Em transito"
    ainda sem mercadoria chegada, ou "Recebido" sem a tabela -- os dois
    casos vistos ao vivo).

    Nao ha coluna EAN nesta tabela -- ean_linha sempre vazio.
    """
    soup = BeautifulSoup(html, "html.parser")
    tabela = None
    for t in soup.find_all("table"):
        cabecalhos = [th.get_text(strip=True).upper() for th in t.find_all("th")]
        if ("PRODUTO" in cabecalhos and "LOTE" in cabecalhos
                and "VALIDADE" in cabecalhos and "QUANTIDADE" in cabecalhos):
            tabela = t
            break
    if tabela is None:
        return []

    itens = []
    for tr in tabela.find_all("tr"):
        celulas = tr.find_all("td")
        if len(celulas) < 6:
            continue  # cabecalho ou linha de rodape
        produto = celulas[0].get_text(" ", strip=True)
        lote = celulas[1].get_text(" ", strip=True)
        validade = celulas[4].get_text(" ", strip=True)
        bruta = celulas[5].get_text(" ", strip=True).replace(".", "").replace(",", ".")
        try:
            quantidade = float(bruta)
        except ValueError:
            continue  # linha de total ou celula vazia
        m = RE_SKU_DESCRICAO.match(produto)
        sku, descricao = (m.group(1), m.group(2).strip()) if m else ("", produto)
        itens.append({
            "linha": len(itens) + 1,
            "sku": sku,
            "ean_linha": "",
            "descricao": descricao,
            "qtd_embalagem": quantidade,
            "lote": lote,
            "validade": validade,
        })
    return itens


def ler_itens(sessao: StokkiSession, id_recebimento: int) -> list[dict]:
    """GET no detalhe do recebimento + extrair_itens_do_recebimento."""
    resp = sessao.get(f"{BASE_URL}/pt-br/administrator/inventory/incoming/show/{id_recebimento}")
    resp.raise_for_status()
    return extrair_itens_do_recebimento(resp.text)
