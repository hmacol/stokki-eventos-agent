# -*- coding: utf-8 -*-
"""
stokki/listagem.py

Em 24/11/2026 (~13h) a Stokki trocou a tela de listagem de pedidos de saida
e de recebimentos:

  antes: GET .../inventory/outbound/table   e  .../inventory/incoming/table
  agora: GET .../inventory/outbound/listing/table  e  .../incoming/listing/table

O endereco antigo NAO da 404: redireciona pro /login (a Stokki faz isso com
qualquer rota que nao existe), e a StokkiSession entendia "sessao expirada",
relogava e desistia. Foram centenas de logins no dia e toda rotina que lista
pedido parou.

Alem do endereco, mudou o formato de cada linha do aaData. Antes cada campo
era um pedaco de HTML; agora e JSON estruturado (medido em producao, 24/11):

  id            45080                      (numero; antes '<a href=".../show/45080">#PS-45080</a><br>REF')
  reference     '#PS-45080' / '#PE-2661'
  show_url      'https://.../outbound/show/45080'
  references    [{'label': 'REF', 'value': '390512'}]
  created_at / expedition_date / arrival_date / receipt_date
                {'date': '25/11/2026', 'time': None}  ou None
  client        {'name': 'MARIA DOLORES ...', 'code': '#stkkc-48'}   (nome inteiro, nao mais truncado)
  origin / destination   {'name': ..., 'place': 'Sao Paulo, SP'}
  carrier       {'name': 'FRESHLOG', 'document': '54.039.899/0001-85'}  ou None
  markers       [{'label': 'Aguardando Faturamento', 'color', 'icon', 'title'}]
  state         {'key': 'Waiting for Carrier', 'label': 'Aguardando Transportador', 'on_hold': False, ...}
  actions       {'button': {...}, 'items': [{'key': 'danfe', 'label': 'Gerar DANFE',
                 'class': 'btn_danfe', 'url': None, 'data': {'url': 'users/.../x.xml'}}, ...]}
  selection     {'cancel': True}

Umas vinte rotinas leem essas linhas no formato antigo (regex de /show/<id>,
#stkkc-<id> dentro de 'client', btn_danfe data-url dentro de 'action'...).
converter_linha devolve a linha nova NO FORMATO ANTIGO, pra nenhuma delas
precisar mudar. listar_pedidos e listar_recebimentos ja chamam.

O que o endereco novo passou a RECUSAR (HTTP 422, antes aceitava):
  - length acima de 100;
  - state fora da lista da tela. Valem: "" (todos os abertos) e, na saida,
    "Open", "Separating", "Ready to Pack", "Waiting for Carrier", "On hold";
    no recebimento, "In transit", "Awaiting receipt", "Receipt started",
    "Received", "On hold". "Sent", "Canceled" e "all" NAO valem mais: a
    listagem nova so mostra pedido em aberto;
  - carrier que nao seja o id numerico da transportadora (#stkkt-<id>).
"""
import html

# so os atributos que o HTML antigo trazia e que alguem le (trocar_nf: btn_danfe
# data-url). Os outros ids soltos do JSON novo (warehouse_id, client_id...)
# ficam de fora de proposito: quem procura o numero da NF no texto da linha
# nao pode esbarrar neles.
_ATRIBUTOS_DE_ACAO = ("url", "id", "action", "target")


class ErroListagemStokki(RuntimeError):
    """A Stokki recusou o pedido de listagem (HTTP 422) -- a mensagem diz o motivo."""


def conferir_resposta(resp) -> None:
    """Levanta ErroListagemStokki com o motivo que a Stokki deu quando ela
    recusa os parametros (422). Sem isso o erro chega como um HTTPError mudo."""
    if getattr(resp, "status_code", None) != 422:
        return
    try:
        erros = resp.json().get("errors") or {}
        motivo = "; ".join(f"{campo}: {' '.join(msgs)}" for campo, msgs in erros.items())
    except Exception:  # noqa: BLE001 -- corpo ilegivel: vai o texto cru
        motivo = ""
    raise ErroListagemStokki(f"a Stokki recusou a listagem (422): {motivo or (getattr(resp, 'text', '') or '')[:200]}")


def _e(valor) -> str:
    return html.escape(str(valor), quote=True)


def _data(campo) -> str:
    if isinstance(campo, dict):
        return " ".join(str(campo.get(k)) for k in ("date", "time") if campo.get(k))
    return str(campo or "")


def _duas_linhas(campo, primeira: str, segunda: str) -> str:
    if isinstance(campo, dict):
        partes = [_e(campo.get(k)) for k in (primeira, segunda) if campo.get(k)]
        return "\n<br>".join(partes)
    return str(campo or "")


def _acoes(campo) -> str:
    if not isinstance(campo, dict):
        return str(campo or "")
    saida = []
    for item in campo.get("items") or []:
        if not isinstance(item, dict):
            continue
        dados = item.get("data") if isinstance(item.get("data"), dict) else {}
        attrs = "".join(f' data-{k}="{_e(dados[k])}"' for k in _ATRIBUTOS_DE_ACAO if dados.get(k) not in (None, ""))
        saida.append(f'<a class="dropdown-item {_e(item.get("class") or "")}" href="{_e(item.get("url") or "#")}"'
                     f' data-key="{_e(item.get("key") or "")}"{attrs}>{_e(item.get("label") or "")}</a>')
    return "".join(saida)


def e_linha_nova(linha) -> bool:
    return isinstance(linha, dict) and (isinstance(linha.get("id"), int) or "show_url" in linha)


def converter_linha(linha):
    """Linha do formato novo -> dict com as chaves e o HTML do formato antigo.
    Linha que ja esta no formato antigo (ou nao e dict) volta como veio."""
    if not e_linha_nova(linha):
        return linha
    id_ = linha.get("id")
    codigo = linha.get("reference") or ""
    refs = [str(r.get("value")) for r in (linha.get("references") or [])
            if isinstance(r, dict) and r.get("value") not in (None, "")]
    cliente = linha.get("client")
    estado = linha.get("state")
    antiga = {
        "checkbox": f'<input type="checkbox" value="{_e(id_)}">',
        # mesma forma de antes: link com o id que abre o detalhe, o codigo, e a
        # Ref. do Pedido (= numero da NF quando o pedido nasceu de XML) embaixo
        "id": f'<a href="{_e(linha.get("show_url") or f"/show/{id_}")}">{_e(codigo)}</a>'
              + "".join(f'<br>\n<span class="text-muted">{_e(r)}</span>' for r in refs),
        "type": str(linha.get("type") or ""),
        "motion": str(linha.get("motion") or ""),
        "client": (f'{_e(cliente.get("name") or "")} <span class="text-muted">{_e(cliente.get("code") or "")}</span>'
                   if isinstance(cliente, dict) else str(cliente or "")),
        "origin": _duas_linhas(linha.get("origin"), "name", "place"),
        "destination": _duas_linhas(linha.get("destination"), "name", "place"),
        "marker": " ".join(f'<span class="badge" title="{_e(m.get("title") or "")}">{_e(m.get("label") or "")}</span>'
                           for m in (linha.get("markers") or []) if isinstance(m, dict)),
        "state": _e(estado.get("label") or estado.get("key") or "") if isinstance(estado, dict) else str(estado or ""),
        "action": _acoes(linha.get("actions")),
    }
    for campo in ("expedition_date", "arrival_date", "receipt_date", "created_at", "order_date"):
        if campo in linha:
            antiga[campo] = _data(linha[campo])
    if "carrier" in linha:
        antiga["carrier"] = _duas_linhas(linha["carrier"], "name", "document")
    return antiga


def converter_resposta(dados):
    """Converte o aaData inteiro de uma resposta da listagem (no lugar)."""
    if isinstance(dados, dict) and isinstance(dados.get("aaData"), list):
        dados["aaData"] = [converter_linha(l) for l in dados["aaData"]]
    return dados
