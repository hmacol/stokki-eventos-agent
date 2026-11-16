# -*- coding: utf-8 -*-
"""
stokki/expedicao_programada.py -- Expedicao Programada (#OE-n) da Stokki,
pela conta provider (Hugo, 12/11).

Pedido dentro de uma OE aberta ("Aguardando Expedicao") fica "Aguardando
Transportador" e o shipping/store responde 404 "situacao invalida": o
expedir_pedidos.py nunca consegue expedir. Aqui o pedido e solto:

  - OE so com ele  -> finaliza a OE (mesmo POST do "Finalizar Expedicao com
    Romaneio" da tela Expedicoes Programadas, sem imprimir e sem arquivo);
    a Stokki ja deixa o pedido "Enviado".
  - OE com outros  -> tira so ele da OE ("Remover Pedidos"); quem chama
    expede em seguida pelo caminho normal. Os outros ficam como estavam.

Todas as funcoes recebem a `page` do Playwright ja logada no /provider/
(expedir_pedidos._setup_playwright) e usam fetch() do proprio navegador,
como expedir_pedidos.expedir_na_stokki. Endpoints lidos da tela em 12/11
(sonda so de leitura).
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

BASE = "https://freshlog.stokki.com.br/pt-br/provider"
URL_PEDIDO = BASE + "/inventory/outbound/show/{id}"
URL_OE = BASE + "/operation/shipping/show/{oe}"
URL_FINALIZAR = BASE + "/operation/shipping/scheduled/finish"
URL_REMOVER = BASE + "/operation/shipping/scheduled/remove"

_RE_OE = re.compile(r"#OE-(\d+)")
_RE_PS = re.compile(r"#PS-(\d+)")
_RE_TOTAL = re.compile(r"de\s+(\d+)\s+pedido", re.IGNORECASE)


def oe_do_pedido(page, id_stokki) -> int | None:
    """Numero da OE em que o pedido esta (link "#OE-n" da pagina do pedido); None se nao esta em nenhuma."""
    page.goto(URL_PEDIDO.format(id=id_stokki), wait_until="networkidle", timeout=40_000)
    page.wait_for_timeout(500)
    textos = page.evaluate("() => [...document.querySelectorAll('a[href*=\"/operation/shipping/show/\"]')]"
                           ".map(a => a.innerText.trim())")
    for t in textos:
        m = _RE_OE.search(t or "")
        if m:
            return int(m.group(1))
    return None


def pedidos_da_oe(page, oe: int) -> tuple[list[int], int]:
    """(ids dos pedidos listados na pagina da OE, total que a tabela informa)."""
    page.goto(URL_OE.format(oe=oe), wait_until="networkidle", timeout=40_000)
    page.wait_for_timeout(1000)
    dados = page.evaluate("""() => ({
        links: [...document.querySelectorAll('table a')].map(a => a.innerText.trim()),
        info: [...document.querySelectorAll('.dataTables_info')].map(e => e.innerText).join(' ')
    })""")
    ids = []
    for t in dados["links"]:
        m = _RE_PS.match(t or "")
        if m and int(m.group(1)) not in ids:
            ids.append(int(m.group(1)))
    m = _RE_TOTAL.search(dados["info"] or "")
    return ids, int(m.group(1)) if m else len(ids)


def _post(page, url: str, campos: list) -> tuple[bool, str]:
    """POST multipart pelo fetch do navegador. (ok, erro): ok = HTTP 2xx, como o success do $.ajax da tela."""
    r = page.evaluate(
        """async ([url, campos]) => {
            const meta = document.querySelector('meta[name=csrf-token]');
            let csrf = meta && meta.content ? meta.content : '';
            if (!csrf) {
                const c = document.cookie.split('; ').find(c => c.startsWith('XSRF-TOKEN='));
                csrf = c ? decodeURIComponent(c.split('=').slice(1).join('=')) : '';
            }
            const fd = new FormData();
            fd.append('_token', csrf);
            campos.forEach(([k, v]) => fd.append(k, v));
            try {
                const resp = await fetch(url, {method: 'POST', body: fd,
                    headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}});
                return {status: resp.status, body: await resp.text()};
            } catch (e) {
                return {status: 0, body: String(e)};
            }
        }""",
        [url, campos],
    )
    status, corpo = r.get("status", 0), r.get("body") or ""
    if 200 <= status < 300:
        return True, ""
    try:
        erros = json.loads(corpo).get("errors") or {}
        texto = "; ".join(str(v if not isinstance(v, list) else " ".join(map(str, v))) for v in erros.values())
    except (ValueError, AttributeError):
        texto = ""
    return False, texto or f"HTTP {status}: {corpo[:160]}"


def finalizar_oe(page, oe: int, hora: str) -> tuple[bool, str]:
    """Finaliza a OE inteira (todos os pedidos dela viram "Enviado"). Sem romaneio impresso nem arquivo."""
    return _post(page, URL_FINALIZAR, [["shipment_id[]", str(oe)], ["hour", hora], ["print_waybill", ""],
                                       ["action", "finish"], ["save_signature", "no"], ["name", ""],
                                       ["register", ""], ["telephone", ""], ["plate", ""],
                                       ["observation", ""], ["signature", ""]])


def remover_pedido(page, oe: int, id_stokki) -> tuple[bool, str]:
    """Tira um pedido da OE; a OE e os outros pedidos dela ficam como estavam."""
    return _post(page, URL_REMOVER, [["shipment_id", str(oe)], ["provider_outbound_id", str(id_stokki)], ["row", "0"]])


def liberar_pedido(page, id_stokki, hora: str) -> dict:
    """Solta o pedido da OE em que ele esta. {"acao", "oe", "erro"}; acao:
    sem_oe | oe_finalizada (pedido ja sai Enviado) | removido_da_oe (falta expedir) | falha."""
    id_stokki = int(id_stokki)
    oe = oe_do_pedido(page, id_stokki)
    if not oe:
        return {"acao": "sem_oe", "oe": None, "erro": ""}
    ids, total = pedidos_da_oe(page, oe)
    if id_stokki not in ids and total <= len(ids):
        return {"acao": "falha", "oe": oe, "erro": f"o pedido não aparece na lista da OE-{oe}"}
    if total == 1 and ids == [id_stokki]:
        ok, erro = finalizar_oe(page, oe, hora)
        acao = "oe_finalizada"
    else:
        ok, erro = remover_pedido(page, oe, id_stokki)
        acao = "removido_da_oe"
    logger.info(f"  PS-{id_stokki}: OE-{oe} com {total} pedido(s) -> {acao}: {'ok' if ok else erro}")
    return {"acao": acao if ok else "falha", "oe": oe, "erro": erro}
