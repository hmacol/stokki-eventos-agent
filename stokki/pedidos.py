# -*- coding: utf-8 -*-
"""
stokki/pedidos.py

Leitura de pedidos de saída (outbound) via endpoints internos do Stokki.

Endpoints usados (descobertos via investigação de tráfego de rede):
  GET /pt-br/outbound/count/{status}/all/.../all          — contagem por status
  GET /pt-br/administrator/inventory/outbound/table       — listagem paginada
  GET /pt-br/administrator/inventory/outbound/show/{id}   — detalhe completo
  GET /pt-br/administrator/inventory/outbound/carrier     — diretório de transportadoras
  GET /pt-br/administrator/inventory/outbound/select      — diretório de embarcadores

Estrutura do detalhe de um pedido (página HTML com 5 blocos de endereço):
  - "Cliente:"          → embarcador (dono da mercadoria)
  - "Transportadora:"   → transportadora atribuída (pode ser "Não informado")
  - "Local de Entrega:" → endereço alternativo opcional (pode ser "Não informado")
  - "Origem:"           → armazém de saída (Freshlog, fixo)
  - "Destino:"          → destinatário final (nome, CPF/CNPJ, endereço, telefone)

Estrutura de um bloco de endereço retornado por _parsear_bloco_address():
  {
    "nome":         str,
    "documento":    str,   # CPF ou CNPJ, como vem no HTML
    "endereco":     str,   # logradouro + número + complemento
    "cidade":       str,
    "uf":           str,
    "cep":          str,
    "telefone":     str,
    "email":        str,
  }
"""
import json
import logging
import re
import time
from typing import Iterator

from bs4 import BeautifulSoup

from stokki.auth import StokkiSession

logger = logging.getLogger(__name__)

BASE_URL = "https://freshlog.stokki.com.br"

# Status de pedido conhecidos no Stokki (valor exato usado na URL)
STATUS_AGUARDANDO_TRANSPORTADOR = "Waiting for Carrier"
STATUS_TODOS = "all"


# ── Contagem ─────────────────────────────────────────────────────────────────

def contar_pedidos(sessao: StokkiSession, status: str = STATUS_TODOS) -> dict:
    """
    Retorna os contadores de pedidos agrupados por status.
    status: usar STATUS_AGUARDANDO_TRANSPORTADOR ou STATUS_TODOS (ou "all").
    """
    url = f"{BASE_URL}/pt-br/outbound/count/{status}/all/all/all/all/all/all/all/all"
    resp = sessao.get(url)
    resp.raise_for_status()
    return resp.json()


# ── Listagem paginada ─────────────────────────────────────────────────────────

def listar_pedidos(
    sessao: StokkiSession,
    status: str = STATUS_AGUARDANDO_TRANSPORTADOR,
    cliente: str = "",
    transportadora: str = "",
    busca: str = "",
    pagina: int = 0,
    por_pagina: int = 100,
) -> dict:
    """
    Retorna uma página de pedidos no formato DataTables.

    Retorna dict com:
      {
        "draw": int,
        "recordsTotal": int,        # total de registros (sem filtro)
        "recordsFiltered": int,     # total com os filtros aplicados
        "aaData": [                 # lista de pedidos nesta página
          [id_interno, codigo_ps, tipo, embarcador, origem, destino,
           transportadora, marcador, situacao, acoes_html],
          ...
        ]
      }
    """
    # Parâmetros exatos que o browser envia — o servidor valida todas as
    # 12 colunas, os campos order/search e o unit. Qualquer subconjunto
    # resulta em 500 (confirmado via debug_url_completa.py).
    import time as _time
    params = {
        "draw": 1,
        "_": int(_time.time() * 1000),
        # colunas (0–11) — exatamente como o DataTables gera
        "columns[0][data]": "checkbox",  "columns[0][name]": "",  "columns[0][searchable]": "true",  "columns[0][orderable]": "false",  "columns[0][search][value]": "",  "columns[0][search][regex]": "false",
        "columns[1][data]": "id",         "columns[1][name]": "",  "columns[1][searchable]": "true",  "columns[1][orderable]": "true",   "columns[1][search][value]": "",  "columns[1][search][regex]": "false",
        "columns[2][data]": "expedition_date", "columns[2][name]": "", "columns[2][searchable]": "true", "columns[2][orderable]": "true",  "columns[2][search][value]": "", "columns[2][search][regex]": "false",
        "columns[3][data]": "type",       "columns[3][name]": "",  "columns[3][searchable]": "true",  "columns[3][orderable]": "false",  "columns[3][search][value]": "",  "columns[3][search][regex]": "false",
        "columns[4][data]": "motion",     "columns[4][name]": "",  "columns[4][searchable]": "true",  "columns[4][orderable]": "false",  "columns[4][search][value]": "",  "columns[4][search][regex]": "false",
        "columns[5][data]": "client",     "columns[5][name]": "",  "columns[5][searchable]": "true",  "columns[5][orderable]": "false",  "columns[5][search][value]": "",  "columns[5][search][regex]": "false",
        "columns[6][data]": "origin",     "columns[6][name]": "",  "columns[6][searchable]": "true",  "columns[6][orderable]": "false",  "columns[6][search][value]": "",  "columns[6][search][regex]": "false",
        "columns[7][data]": "destination","columns[7][name]": "",  "columns[7][searchable]": "true",  "columns[7][orderable]": "false",  "columns[7][search][value]": "",  "columns[7][search][regex]": "false",
        "columns[8][data]": "carrier",    "columns[8][name]": "",  "columns[8][searchable]": "true",  "columns[8][orderable]": "false",  "columns[8][search][value]": "",  "columns[8][search][regex]": "false",
        "columns[9][data]": "marker",     "columns[9][name]": "",  "columns[9][searchable]": "true",  "columns[9][orderable]": "false",  "columns[9][search][value]": "",  "columns[9][search][regex]": "false",
        "columns[10][data]": "state",     "columns[10][name]": "", "columns[10][searchable]": "true", "columns[10][orderable]": "false", "columns[10][search][value]": "", "columns[10][search][regex]": "false",
        "columns[11][data]": "action",    "columns[11][name]": "", "columns[11][searchable]": "true", "columns[11][orderable]": "false", "columns[11][search][value]": "", "columns[11][search][regex]": "false",
        # ordenação, busca global e paginação
        "order[0][column]": "0",
        "order[0][dir]": "asc",
        "search[value]": "",
        "search[regex]": "false",
        "start": pagina * por_pagina,
        "length": por_pagina,
        # filtros específicos do Stokki
        "state": status,
        "client": cliente,
        "carrier": transportadora,
        "marker": "",
        "unit": "",
        "input_search": busca,
    }
    resp = sessao.get(
        f"{BASE_URL}/pt-br/administrator/inventory/outbound/table",
        params=params,
        headers={"Referer": f"{BASE_URL}/pt-br/administrator/inventory/outbound"},
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except Exception:
        # O Content-Type é text/html mas o corpo é JSON — tenta parsear assim mesmo
        return json.loads(resp.text)


def iterar_todos_pedidos(
    sessao: StokkiSession,
    status: str = STATUS_AGUARDANDO_TRANSPORTADOR,
    cliente: str = "",
    transportadora: str = "",
    busca: str = "",
    por_pagina: int = 100,
    pausa_entre_paginas: float = 0.5,
) -> Iterator[list]:
    """
    Gerador que itera sobre TODOS os pedidos, paginando automaticamente.
    Yield: cada linha (list) do aaData conforme vem da API.
    """
    pagina = 0
    total_vistos = 0

    while True:
        dados = listar_pedidos(
            sessao, status=status, cliente=cliente,
            transportadora=transportadora, busca=busca,
            pagina=pagina, por_pagina=por_pagina,
        )
        linhas = dados.get("aaData", [])
        # O servidor retorna iTotalDisplayRecords (formato legado DataTables)
        # em vez do recordsFiltered mais comum
        total_filtrado = (
            dados.get("recordsFiltered")
            or dados.get("iTotalDisplayRecords")
            or dados.get("iTotalRecords")
            or 0
        )

        if not linhas:
            break

        for linha in linhas:
            yield linha
            total_vistos += 1

        logger.info(
            f"  Página {pagina + 1} — {len(linhas)} pedido(s) "
            f"(total: {total_vistos}/{total_filtrado})"
        )

        if total_vistos >= total_filtrado:
            break

        pagina += 1
        time.sleep(pausa_entre_paginas)


def extrair_id_da_linha(linha) -> int | None:
    """
    Extrai o ID interno do Stokki de uma linha do aaData.
    O campo 'id' contém HTML com link /show/{id} — usamos o href como
    âncora confiável em vez de tentar parsear o texto do campo.
    """
    if isinstance(linha, dict):
        # Campo sintético injetado pelo pipeline no modo --pedido
        if "_codigo_ps" in linha:
            m = re.search(r"/show/(\d+)", linha.get("id", ""))
            if m:
                return int(m.group(1))

        # Procura /show/{id} em qualquer campo — mais confiável que pegar
        # o primeiro número, que pode incluir outros dados concatenados
        for campo in linha.values():
            m = re.search(r"/show/(\d+)", str(campo))
            if m:
                return int(m.group(1))

    if isinstance(linha, list):
        for campo in linha:
            m = re.search(r"/show/(\d+)", str(campo))
            if m:
                return int(m.group(1))

    return None


def extrair_codigo_ps_da_linha(linha) -> str:
    """
    Extrai o código PS-XXXXX de uma linha do aaData.
    Usa o ID do Stokki (extraído do href /show/{id}) como fonte canônica,
    já que os campos de texto da listagem podem conter HTML com múltiplos
    números concatenados que confundem o parser de regex.
    """
    # Campo sintético injetado pelo pipeline no modo --pedido
    if isinstance(linha, dict) and "_codigo_ps" in linha:
        return linha["_codigo_ps"]

    # Usa o ID do /show/ como fonte canônica do código PS
    id_stokki = extrair_id_da_linha(linha)
    if id_stokki:
        return f"PS-{id_stokki}"

    return ""


def extrair_referencia_da_linha(linha) -> str:
    """
    Extrai a referencia (PO) de uma linha do aaData.
    O campo 'id' contem: #PS-XXXXX / referencia / 'Referencias' / 'REF...'
    A referencia e o segundo token de texto limpo do campo.
    """
    if isinstance(linha, dict) and "_codigo_ps" in linha:
        return ""  # linha sintetica do modo --pedido, sem referencia
    id_html = str(linha.get("id", "") if isinstance(linha, dict) else "")
    linhas = [l.strip() for l in re.sub(r"<[^>]+>", "\n", id_html).split("\n")
              if l.strip() and not l.strip().startswith("Refer")]
    # linhas[0] = #PS-XXXXX, linhas[1] = referencia
    return linhas[1] if len(linhas) > 1 else ""


def extrair_transportadora(linha) -> str:
    """
    Extrai o nome da transportadora de uma linha do aaData.
    Útil para pré-filtrar redespacho sem precisar buscar o detalhe.
    """
    if isinstance(linha, dict):
        texto = re.sub(r"<[^>]+>", "", str(linha.get("carrier", ""))).strip()
        return texto
    return ""


# ── Detalhe de um pedido ──────────────────────────────────────────────────────

def obter_detalhe(sessao: StokkiSession, id_pedido: int) -> dict:
    """
    Retorna o detalhe completo de um pedido, com todos os campos parseados.

    Retorna dict com:
      {
        "id":              int,
        "cliente":         dict,   # bloco "Cliente:" — embarcador
        "transportadora":  dict,   # bloco "Transportadora:" (None se não informado)
        "local_entrega":   dict,   # bloco "Local de Entrega:" (None se não informado)
        "origem":          dict,   # bloco "Origem:"
        "destino":         dict,   # bloco "Destino:" — destinatário final
        "mensagens":       list,   # lista de mensagens da aba "Mensagens"
        "owner_id":        str,    # ID da entidade Sale (para envio de mensagens)
        "csrf_token":      str,    # token CSRF extraído da página
      }
    """
    resp = sessao.get(
        f"{BASE_URL}/pt-br/administrator/inventory/outbound/show/{id_pedido}"
    )
    resp.raise_for_status()
    return _parsear_pagina_detalhe(resp.text, id_pedido)


def _parsear_pagina_detalhe(html: str, id_pedido: int) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    # ── Blocos de endereço ─────────────────────────────────────────────────
    blocos = _extrair_blocos_endereco(soup)

    # ── Mensagens ──────────────────────────────────────────────────────────
    mensagens = _extrair_mensagens(soup)

    # ── owner_id (Sale) — necessário para enviar mensagens ─────────────────
    owner_id = ""
    input_owner = soup.find("input", {"name": "owner_id"})
    if input_owner:
        owner_id = input_owner.get("value", "")

    # ── CSRF token ─────────────────────────────────────────────────────────
    csrf_token = ""
    meta_csrf = soup.find("meta", {"name": "csrf-token"})
    if meta_csrf:
        csrf_token = meta_csrf.get("content", "")

    # Referencia do pedido (PO) -- aparece como "Ref. do Pedido:" na tabela
    referencia = ""
    m_ref = re.search(r"Ref\.\s*do\s*Pedido:\s*</th>\s*<td>([^<]+)</td>", html, re.IGNORECASE)
    if m_ref:
        referencia = m_ref.group(1).strip()

    # stkkc_id do embarcador -- aparece como (#stkkc-XX) na tabela de detalhes
    stkkc_id = None
    m_stkkc = re.search(r"#stkkc-(\d+)", html)
    if m_stkkc:
        stkkc_id = int(m_stkkc.group(1))

    return {
        "id":         id_pedido,
        "referencia": referencia,
        "stkkc_id":   stkkc_id,
        "cliente":    blocos.get("cliente"),
        "transportadora": blocos.get("transportadora"),
        "local_entrega": blocos.get("local_entrega"),
        "origem": blocos.get("origem"),
        "destino": blocos.get("destino"),
        "mensagens": mensagens,
        "owner_id": owner_id,
        "csrf_token": csrf_token,
    }


def _extrair_blocos_endereco(soup: BeautifulSoup) -> dict:
    """
    Encontra os 5 blocos de endereço da página pelo rótulo de texto
    que precede cada <address>.

    Rótulos esperados → chave no dict retornado:
      "Cliente:"          → "cliente"
      "Transportadora:"   → "transportadora"
      "Local de Entrega:" → "local_entrega"
      "Origem:"           → "origem"
      "Destino:"          → "destino"
    """
    MAPA_ROTULOS = {
        "cliente":         ["Cliente:"],
        "transportadora":  ["Transportadora:"],
        "local_entrega":   ["Local de Entrega:"],
        "origem":          ["Origem:"],
        "destino":         ["Destino:"],
    }
    resultado = {k: None for k in MAPA_ROTULOS}

    # Busca todos os .invoice-col que contêm um <address>
    for div in soup.find_all("div", class_="invoice-col"):
        texto_div = div.get_text(separator=" ", strip=True)
        address_tag = div.find("address")
        if not address_tag:
            continue

        for chave, rotulos in MAPA_ROTULOS.items():
            if any(r in texto_div for r in rotulos):
                resultado[chave] = _parsear_bloco_address(address_tag)
                break

    return resultado


def _parsear_bloco_address(tag) -> dict | None:
    """
    Parseia uma tag <address> do Stokki para um dict estruturado.

    Formato esperado (separado por <br>):
      <strong>Nome</strong>
      CPF/CNPJ
      Logradouro, Número - Complemento, Bairro
      Cidade - UF, CEP
      Telefone: (XX) XXXXX-XXXX
      E-mail: xxx@xxx.com

    Retorna None se o bloco indicar "Não informado".
    """
    texto_bruto = tag.get_text(separator="\n", strip=True)
    if not texto_bruto or "Não informado" in texto_bruto:
        return None

    # Divide pelas quebras de linha reais do HTML (cada <br> vira \n)
    linhas = [l.strip() for l in texto_bruto.split("\n") if l.strip()]

    resultado = {
        "nome": "",
        "documento": "",
        "endereco": "",
        "cidade": "",
        "uf": "",
        "cep": "",
        "telefone": "",
        "email": "",
        "_raw": texto_bruto,  # preserva o texto bruto pra debug/LLM
    }

    for i, linha in enumerate(linhas):
        if i == 0:
            resultado["nome"] = linha
            continue

        if re.match(r"[\d.\-/]+$", linha.replace(" ", "")) and not resultado["documento"]:
            # CPF (XXX.XXX.XXX-XX) ou CNPJ (XX.XXX.XXX/XXXX-XX)
            resultado["documento"] = linha
            continue

        if linha.startswith("Telefone:"):
            resultado["telefone"] = linha.replace("Telefone:", "").strip()
            continue

        if linha.startswith("E-mail:") or linha.startswith("Email:"):
            resultado["email"] = re.sub(r"E-?mail:", "", linha, flags=re.IGNORECASE).strip()
            continue

        # Linha "Cidade - UF, CEP" ou "Cidade - UF"
        m_cidade = re.match(
            r"^(.+?)\s*[-–]\s*([A-Z]{2})\s*,?\s*([\d.\-]+)?$", linha
        )
        if m_cidade and not resultado["cidade"]:
            resultado["cidade"] = m_cidade.group(1).strip()
            resultado["uf"] = m_cidade.group(2).strip()
            resultado["cep"] = (m_cidade.group(3) or "").strip()
            continue

        # Tudo mais é endereço (logradouro, número, complemento)
        if not resultado["endereco"]:
            resultado["endereco"] = linha

    return resultado


def _extrair_mensagens(soup: BeautifulSoup) -> list:
    """
    Extrai as mensagens da aba 'Mensagens' da página de detalhe.

    Retorna lista de dicts:
      [
        {
          "usuario": str,
          "tipo_usuario": str,   # "Cliente", "Operador", etc.
          "data": str,
          "texto": str,
        },
        ...
      ]
    """
    mensagens = []

    div_posts = soup.find("div", id="div_posts")
    if not div_posts:
        return mensagens

    for post in div_posts.find_all("div", class_="post"):
        usuario_span = post.find("span", class_="text-stokki")
        tipo_span = post.find("small")
        descricao_span = post.find("span", class_="description")
        texto_p = post.find("p")

        usuario = usuario_span.get_text(strip=True) if usuario_span else ""
        tipo = tipo_span.get_text(strip=True).strip("()") if tipo_span else ""
        descricao = descricao_span.get_text(strip=True) if descricao_span else ""
        texto = texto_p.get_text(strip=True) if texto_p else ""

        # Extrai a data da descrição "Enviou uma mensagem - DD/MM/AAAA - HH:MMhrs"
        data = ""
        m_data = re.search(r"(\d{2}/\d{2}/\d{4}[\s\-]+\d{2}:\d{2}[^\s]*)", descricao)
        if m_data:
            data = m_data.group(1).strip()

        if texto:  # ignora posts sem conteúdo
            mensagens.append({
                "usuario": usuario,
                "tipo_usuario": tipo,
                "data": data,
                "texto": texto,
            })

    return mensagens


# ── Diretórios ────────────────────────────────────────────────────────────────

def listar_transportadoras(sessao: StokkiSession) -> list[dict]:
    """
    Retorna a lista de transportadoras cadastradas no Stokki.
    Cada item: {"name": str, "cnpj": str}
    """
    resp = sessao.get(
        f"{BASE_URL}/pt-br/administrator/inventory/outbound/carrier"
    )
    resp.raise_for_status()
    dados = resp.json()
    return dados.get("carriers", dados if isinstance(dados, list) else [])


def listar_embarcadores(sessao: StokkiSession) -> list:
    """
    Retorna a lista de embarcadores/clientes cadastrados no Stokki.
    """
    resp = sessao.get(
        f"{BASE_URL}/pt-br/administrator/inventory/outbound/select"
    )
    resp.raise_for_status()
    return resp.json()
