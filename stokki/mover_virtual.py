# -*- coding: utf-8 -*-
"""Movimentacao de Estoque Manual (area provider da Stokki): devolve saldo do endereco Virtual.

Segue o script provado em 03/11 (PS-43101, adjust_id 110832/110833): pagina `adjust`, busca do
Virtual, do destino, do produto, check do destino, POST adjust/store/process e poll adjust/processed.
Um login para todos os itens. NAO pega trava: quem chama ja esta com a conta provider.
Nunca levanta por resposta da Stokki (vira `erro` no resultado); so deixa subir falha de login/navegador.
Nunca move mais que `qtd` de um item nem mais que a tela oferece. Poll sem confirmacao = nao concluido.
"""
import json
import logging
import os
import time
from urllib.parse import quote

from stokki.devolucao_estoque import quanto_mover

logger = logging.getLogger(__name__)

BASE = "https://freshlog.stokki.com.br/pt-br/provider/operation/inventory"
POLL_INTERVALO = 3
POLL_TENTATIVAS = 30          # 3 s x 30 = 90 s
TOLERANCIA = 0.0005
CHAVES_TEXTO_DO_ENDERECO = ("code", "name", "description", "label")


def montar_campos(cliente_id, id_virtual, product_id, oferecido, qtd, id_destino, motivo) -> dict:
    """Campos do form_adjust (sem o _token, que vem da pagina)."""
    return {"client_id": cliente_id, "origin_location_id": id_virtual, "product_id": product_id,
            "quantity_available": oferecido, "quantity": qtd, "destination_location_id": id_destino,
            "damaged": 0, "reason": motivo}


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _txt(v) -> str:
    """Numero como a tela espera (4.0 -> '4')."""
    n = _num(v)
    if n is not None and n == int(n):
        return str(int(n))
    return str(v)


def _id_local(resp):
    loc = resp.get("location") if isinstance(resp, dict) else None
    return loc.get("id") if isinstance(loc, dict) else None


def _destino_confere(resp, endereco: str) -> bool:
    """A busca pode devolver endereco parecido (FAB-1 x FAB-10): a resposta precisa trazer o texto do endereco
    e um deles tem de ser exatamente o codigo pedido. Sem nenhuma chave de texto conhecida: recusa e loga as
    chaves que vieram (a prova real mostra o nome certo)."""
    loc = resp.get("location") if isinstance(resp, dict) else None
    textos = [str(loc[k]).strip().casefold() for k in CHAVES_TEXTO_DO_ENDERECO
              if isinstance(loc, dict) and isinstance(loc.get(k), str)]
    if not textos:
        chaves = sorted(loc) if isinstance(loc, dict) else loc
        logger.warning(f"mover_virtual: resposta do destino {endereco} sem chave de texto conhecida "
                       f"{CHAVES_TEXTO_DO_ENDERECO}; chaves que vieram: {chaves}")
        return False
    return endereco.strip().casefold() in textos


def _oferecido(resp, product_id: int):
    """Quanto a tela deixa tirar do Virtual para o produto. `quantity_available` manda, mesmo 0; so sem ele
    (chave ausente) cai para `quantity`, que e o total do Virtual compartilhado."""
    if not isinstance(resp, dict):
        return None
    lista = resp.get("products") or ([resp["product"]] if resp.get("product") else [])
    achado = next((x for x in lista if isinstance(x, dict) and str(x.get("id")) == str(product_id)), None)
    if not achado:
        return None
    if achado.get("quantity_available") is not None:
        return _num(achado["quantity_available"])
    return _num(achado.get("quantity"))


def _concluido(resp) -> bool:
    return isinstance(resp, dict) and bool(resp.get("finished") or resp.get("processed")
                                           or resp.get("status") in ("finished", "processed", "success"))


def _int(v):
    """adjust_id como int (a Stokki pode mandar texto); o que nao converte vira None."""
    if isinstance(v, bool):
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _recusa(st) -> bool:
    """Status que e a tela dizendo nao (404/422 e outros 4xx de validacao); 401/403/419/429 e 5xx sao tecnicos."""
    return isinstance(st, int) and 400 <= st < 500 and st not in (401, 403, 419, 429)


def _res(m, destino, pedido, movido=0, adjust_id=None, erro="", **marcas):
    return {"product_id": m["product_id"], "destino": destino, "pedido": pedido, "movido": movido,
            "adjust_id": adjust_id, "erro": erro, **marcas}


def _executar(api, cliente_id: int, movimentos: list, motivo: str, ao_concluir=None, dormir=time.sleep) -> list:
    """Conversa com a tela por `api` (get(caminho) -> (status, json|texto); post(campos) -> (status, texto);
    token). Um resultado por movimento tentado; `ao_concluir` roda depois de cada um. Resultado `tecnico`
    (sessao caiu, tela fora do ar: nada foi gravado) ou `incerto` (nao se sabe se gravou) PARA tudo.
    Falta de saldo e recusa de destino nao: seguem para o proximo candidato (ou encerram o item)."""
    resultados = []

    def fim(r):
        resultados.append(r)
        if ao_concluir:
            ao_concluir(r)

    st, orig = api.get(f"adjust/location/search/{cliente_id}/VIRTUAL")
    id_virtual = _id_local(orig) if st == 200 else None
    if not id_virtual:
        fim(_res(movimentos[0], "", movimentos[0]["qtd"], tecnico=True,
                 erro=f"busca do endereco Virtual falhou (status {st})"))
        return resultados
    for m in movimentos:
        restante = m["qtd"]
        for endereco, parte in m["destinos"]:
            if restante <= TOLERANCIA:
                break
            alvo = min(parte, restante)
            st, dest = api.get(f"adjust/destination/search/0/{quote(endereco, safe='')}")
            if st != 200 and not _recusa(st):
                fim(_res(m, endereco, alvo, tecnico=True, erro=f"busca do destino {endereco} falhou (status {st})"))
                return resultados
            id_destino = _id_local(dest) if st == 200 else None
            if not id_destino or not _destino_confere(dest, endereco):
                fim(_res(m, endereco, alvo, erro=f"destino {endereco} nao encontrado ou diferente do pedido (status {st})"))
                continue
            st, prod = api.get(f"adjust/product/search/{cliente_id}/{id_virtual}/{quote(m['sku'], safe='')}")
            if st != 200:
                fim(_res(m, endereco, alvo, tecnico=True, erro=f"busca do produto {m['sku']} falhou (status {st})"))
                return resultados
            oferecido = _oferecido(prod, m["product_id"])
            quantidade = quanto_mover(alvo, oferecido) if oferecido is not None else 0
            if quantidade <= 0:
                fim(_res(m, endereco, alvo, erro=f"Virtual nao oferece saldo de {m['sku']}"))
                break
            st, _chk = api.get(f"adjust/check/location/{id_destino}/{m['product_id']}")
            if st != 200:
                if not _recusa(st):
                    fim(_res(m, endereco, alvo, tecnico=True, erro=f"conferencia do destino {endereco} falhou (status {st})"))
                    return resultados
                fim(_res(m, endereco, alvo, erro=f"destino {endereco} recusado na conferencia (status {st})"))
                continue
            campos = montar_campos(cliente_id, id_virtual, m["product_id"], oferecido, quantidade, id_destino, motivo)
            campos = {"_token": api.token, **{k: _txt(v) for k, v in campos.items()}}
            st, corpo = api.post(campos)
            try:
                adjust_id = _int(json.loads(corpo).get("adjust_id"))
            except (ValueError, TypeError, AttributeError):
                adjust_id = None
            if adjust_id is None:
                if st in (401, 403, 419, 429):
                    fim(_res(m, endereco, alvo, tecnico=True, erro=f"store/process negado (status {st}); nada gravado"))
                    return resultados
                if _recusa(st):
                    fim(_res(m, endereco, alvo, erro=f"store/process recusado (status {st}): {str(corpo)[:200]}"))
                    continue   # a tela disse nao: nada foi gravado, vale o proximo destino
                fim(_res(m, endereco, alvo, incerto=True,
                         erro=f"store/process sem resposta clara (status {st}): {str(corpo)[:200]}"))
                return resultados   # nao se sabe se gravou: nao repetir
            confirmado = False
            for _ in range(POLL_TENTATIVAS):
                dormir(POLL_INTERVALO)
                st, proc = api.get(f"adjust/processed/{adjust_id}")
                if st == 200 and _concluido(proc):
                    confirmado = True
                    break
            if not confirmado:
                fim(_res(m, endereco, alvo, 0, adjust_id, f"movimentacao {adjust_id} ainda em processamento",
                         incerto=True))
                return resultados
            fim(_res(m, endereco, alvo, quantidade, adjust_id))
            restante -= quantidade
            if quantidade < alvo - TOLERANCIA:
                break   # a tela ofereceu menos: leva o que deu e encerra o item (parcial)
    return resultados


class _ApiPagina:
    """As chamadas saem de dentro da pagina logada (mesmos cabecalhos do script provado)."""

    def __init__(self, page, token):
        self.page, self.token = page, token

    def get(self, caminho):
        r = self.page.request.get(f"{BASE}/{caminho}", headers={"X-Requested-With": "XMLHttpRequest",
                                                              "Accept": "application/json"})
        try:
            return r.status, r.json()
        except Exception:  # noqa: BLE001 -- resposta que nao e JSON vira texto
            return r.status, r.text()[:300]

    def post(self, campos):
        resp = self.page.evaluate("""async (d) => {
            const fd = new FormData();
            for (const [k, v] of Object.entries(d.campos)) fd.append(k, v);
            const r = await fetch(d.url, {method: 'POST', body: fd, credentials: 'same-origin',
                headers: {'X-Requested-With': 'XMLHttpRequest', 'Accept': 'application/json'}});
            return {status: r.status, body: await r.text()};
        }""", {"url": f"{BASE}/adjust/store/process", "campos": campos})
        return resp["status"], resp["body"]


def mover(config, cliente_id: int, movimentos: list, motivo: str, headless: bool = True, ao_concluir=None) -> list:
    """Movimentos [{product_id, sku, qtd, destinos: [(endereco, qtd)]}] -> um resultado por movimento tentado
    {product_id, destino, pedido, movido, adjust_id, erro}. Um login, depois fecha o navegador."""
    if os.environ.get("FRESH_AMBIENTE", "").strip().lower() == "homolog":
        raise RuntimeError("indisponivel na homologacao: movimentacao na Stokki")
    from playwright.sync_api import sync_playwright
    from stokki import estacao_impressao as ei
    usuario, senha = ei._credenciais_provider(config)
    if not usuario or not senha:
        raise ValueError("credenciais do provider nao encontradas no config")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        try:
            page = ei._novo_contexto_disfarcado(browser).new_page()
            page.goto(f"{BASE}/adjust", wait_until="domcontentloaded", timeout=45_000)
            ei._preencher_form_login(page, usuario, senha)
            page.goto(f"{BASE}/adjust", wait_until="domcontentloaded", timeout=45_000)
            token = page.eval_on_selector("#form_adjust input[name=_token]", "e => e.value")
            return _executar(_ApiPagina(page, token), cliente_id, movimentos, motivo, ao_concluir)
        finally:
            browser.close()
