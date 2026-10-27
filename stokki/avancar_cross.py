# -*- coding: utf-8 -*-
"""
stokki/avancar_cross.py -- avanca pedido Cross (ex.: Quatro Estrelas, item XD,
ja separado pelo embarcador) de "Aberto" ate "Aguardando Transportador" pela
Lista de Pedidos da area de operador da Stokki
(/pt-br/provider/operation/order), clicando os mesmos botoes que um operador:

  etapa "separation"          Aberto        -> Em separacao
  etapa "confirm_separation"  Em separacao  -> (esperado) Aguardando Transportador

Achado em 27/10 pelas sondas stokki/sondar_avancar_status.py e
stokki/sondar_area_operador.py (acoes da lista: separation, confirm_separation,
hold, sent, invoice, tag, carrier, correction_letter, download_documents; o
form_action vai por POST em /provider/inventory/outbound/action).

Seguranca:
  - roda dentro da trava da conta provider (sessao_uso.na_vez_da_provider),
    como a Estacao de Impressao: espera a vez, nao derruba ninguem;
  - SEM --confirmar so abre o modal da etapa, imprime campos/botoes/texto e
    tira print (dados/avancar_cross/) -- nao envia nada;
  - com --confirmar envia o modal, le a mensagem da Stokki e confere a
    Situacao do pedido na pagina do pedido; para no primeiro erro.

Rodar na VPS, como www-data:
    sudo -u www-data venv/bin/python stokki/avancar_cross.py --pedido PS-42622 --etapa separation [--confirmar]
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from stokki import estacao_impressao as ei  # noqa: E402
from stokki.sessao_uso import na_vez_da_provider  # noqa: E402

URL_LISTA = f"{ei.BASE_URL}/pt-br/provider/operation/order"
URL_SHOW = f"{ei.BASE_URL}/pt-br/provider/inventory/outbound/show/{{id}}"
PASTA_PRINTS = _RAIZ / "dados" / "avancar_cross"
ETAPAS = ("iniciar_separacao", "separation", "confirm_separation")
URL_PICKING = f"{ei.BASE_URL}/pt-br/provider/operation/picking/list"


def _sondar_iniciar_separacao(page, pid: int) -> int:
    """Seleção de Pedidos por Lista: abre o modal 'Filtrar por pedido único'
    e mostra os campos, e lê a lista de operadores. Não envia nada."""
    page.goto(URL_PICKING, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    try:
        r = page.request.get(URL_PICKING + "/operator", timeout=30_000)
        corpo = r.text()
        try:
            dados = r.json()
            print("== operadores (/picking/list/operator):", json_resumo(dados))
        except Exception:
            nomes = re.findall(r"<option[^>]*value=\"?([^\" >]*)\"?[^>]*>([^<]+)</option>", corpo)
            print("== operadores (/picking/list/operator):", [(v, " ".join(t.split())) for v, t in nomes][:60] or corpo[:300])
    except Exception as e:
        print("operadores: falha ao ler", type(e).__name__)
    btn = page.locator("#btn_order")
    if btn.count():
        btn.first.click()
        page.wait_for_timeout(2000)
    soup = BeautifulSoup(page.content(), "html.parser")
    form = soup.find("form", id="form_order")
    if form is not None:
        for i in form.find_all(["input", "select"]):
            if i.get("name") and i.get("name") != "_token":
                opcoes = [(o.get("value"), " ".join(o.get_text(" ", strip=True).split())[:30]) for o in i.find_all("option")][:15]
                print(f"   form_order.{i.get('name')}: tipo={i.get('type') or i.name} valor={i.get('value') or ''!s:.30} opcoes={opcoes}")
    for b in soup.select("#form_order button, .modal.show button"):
        print("   botao:", " ".join(b.get_text(" ", strip=True).split())[:40], {k: v for k, v in b.attrs.items() if k in ("id", "type")})
    print("print:", _print(page, f"{pid}_iniciar_separacao"))
    print("== iniciar_separacao: só leitura nesta versão; nada enviado.")
    return 0


def json_resumo(dados) -> str:
    """Resumo curto (só nomes/ids) de uma resposta JSON de operadores."""
    itens = dados if isinstance(dados, list) else next((v for v in dados.values() if isinstance(v, list)), []) if isinstance(dados, dict) else []
    saida = []
    for it in itens[:60]:
        if isinstance(it, dict):
            saida.append({k: it[k] for k in ("id", "name", "nome", "user_id") if k in it})
        else:
            saida.append(it)
    return str(saida) if saida else str(dados)[:300]


def _id_do_pedido(pedido: str) -> int:
    m = re.search(r"(\d+)", pedido or "")
    if not m:
        raise ValueError(f"pedido invalido: {pedido!r}")
    return int(m.group(1))


def _print(page, nome: str) -> str:
    PASTA_PRINTS.mkdir(parents=True, exist_ok=True)
    caminho = PASTA_PRINTS / f"{datetime.now():%Y%m%d_%H%M%S}_{nome}.png"
    page.screenshot(path=str(caminho), full_page=True)
    return str(caminho)


def situacao_do_pedido(page, pid: int) -> str:
    """Le a 'Situacao' na pagina do pedido (area de operador)."""
    page.goto(URL_SHOW.format(id=pid), wait_until="domcontentloaded", timeout=45_000)
    ei._aguardar_pagina_acalmar(page)
    soup = BeautifulSoup(page.content(), "html.parser")
    rotulo = soup.find(string=re.compile(r"Situa"))
    if not rotulo:
        return "?"
    texto = " ".join(rotulo.find_parent().find_parent().get_text(" ", strip=True).split())
    m = re.search(r"Situa[çc][ãa]o:?\s*(.+)", texto)
    return (m.group(1) if m else texto)[:60]


def _abrir_lista_filtrada(page, pid: int) -> None:
    page.goto(URL_LISTA, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    for seletor in ("#input_search", "input[name=input_search]", "input[placeholder*='esquis']",
                    "input[placeholder*='usca']", "div.dataTables_filter input", "input[type=search]"):
        busca = page.locator(seletor)
        if busca.count() and busca.first.is_visible():
            busca.first.fill(str(pid))
            busca.first.press("Enter")
            page.wait_for_timeout(4000)
            return
    campos = page.evaluate("""() => [...document.querySelectorAll('input,select')].filter(e => e.offsetParent)
        .map(e => `${e.tagName.toLowerCase()} id=${e.id} name=${e.name} ph=${e.placeholder || ''}`)""")
    raise RuntimeError("campo de busca da Lista de Pedidos nao encontrado; campos visiveis: " + "; ".join(campos[:30]))


def _modal_resumo(page) -> dict:
    modal = page.locator("#modal_action")
    html = modal.inner_html() if modal.count() else ""
    soup = BeautifulSoup(html, "html.parser")
    campos = {}
    for i in soup.find_all(["input", "select", "textarea"]):
        nome = i.get("name") or i.get("id")
        if not nome or nome == "_token":
            continue
        visivel = "display: none" not in (i.find_parent(style=True) or {}).get("style", "") if i.find_parent(style=True) else True
        campos[nome] = {"tipo": i.get("type") or i.name, "valor": (i.get("value") or "")[:40], "visivel": visivel,
                        "marcado": i.has_attr("checked")}
    botoes = [" ".join(b.get_text(" ", strip=True).split())[:40] + (" [desabilitado]" if b.has_attr("disabled") else "")
              for b in soup.find_all("button")]
    texto = " ".join(soup.get_text(" ", strip=True).split())[:600]
    return {"campos": campos, "botoes": botoes, "texto": texto}


@na_vez_da_provider
def executar(pedido: str, etapa: str, confirmar: bool) -> int:
    if etapa not in ETAPAS:
        print(f"etapa invalida: {etapa} (use {', '.join(ETAPAS)})")
        return 2
    pid = _id_do_pedido(pedido)
    config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
    usuario, senha = ei._credenciais_provider(config)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = ei._novo_contexto_disfarcado(browser)
            page = context.new_page()
            ei._login_provider(page, usuario, senha)
            antes = situacao_do_pedido(page, pid)
            print(f"== PS-{pid}: situacao antes = {antes}")
            if etapa == "iniciar_separacao":
                return _sondar_iniciar_separacao(page, pid)

            _abrir_lista_filtrada(page, pid)
            item = page.locator(f'[data-action="{etapa}"][data-id="{pid}"]')
            if item.count() == 0:
                acoes = sorted(set(re.findall(rf'data-action="([a-z_]+)"[^>]*data-id="{pid}"|data-id="{pid}"[^>]*data-action="([a-z_]+)"',
                                              page.content())))
                print(f"PARADO: a lista nao oferece '{etapa}' pro PS-{pid}. Acoes vistas pra ele: {acoes}")
                soup = BeautifulSoup(page.content(), "html.parser")
                for el in soup.find_all(True):
                    if any(str(pid) in (v if isinstance(v, str) else " ".join(v)) for v in el.attrs.values()):
                        attrs = {k: (v if isinstance(v, str) else " ".join(v))[:80] for k, v in el.attrs.items()}
                        print("   el", el.name, "|", " ".join(el.get_text(" ", strip=True).split())[:50], "|", attrs)
                linha = next((tr for tr in soup.find_all("tr") if str(pid) in tr.get_text(" ")), None)
                if linha is not None:
                    print("   linha:", [" ".join(td.get_text(" ", strip=True).split())[:30] for td in linha.find_all("td")])
                    for el in linha.find_all(["a", "button", "li", "i", "span"]):
                        attrs = {k: (v if isinstance(v, str) else " ".join(v))[:80] for k, v in el.attrs.items()}
                        if attrs:
                            print("     ", el.name, "|", " ".join(el.get_text(" ", strip=True).split())[:40], "|", attrs)
                else:
                    print(f"   nenhuma linha da tabela com {pid} (o filtro pode nao ter aplicado)")
                print("print:", _print(page, f"{pid}_{etapa}_sem_acao"))
                return 1
            item.first.dispatch_event("click")
            page.wait_for_timeout(2500)
            resumo = _modal_resumo(page)
            print(f"== modal '{etapa}': botoes={resumo['botoes']}")
            for nome, info in resumo["campos"].items():
                print(f"   campo {nome}: {info}")
            print("   texto:", resumo["texto"])
            print("print:", _print(page, f"{pid}_{etapa}_modal"))

            if not confirmar:
                print("== sem --confirmar: nada enviado.")
                return 0

            enviar = page.locator("#form_action button[type=submit], #form_action .processing").first
            if etapa == "confirm_separation" and page.locator("#btn_separation").count():
                enviar = page.locator("#btn_separation").first
            if enviar.count() == 0 or not enviar.is_enabled():
                print("PARADO: botao de envio do modal ausente ou desabilitado.")
                print("print:", _print(page, f"{pid}_{etapa}_botao"))
                return 1
            enviar.click()
            page.wait_for_timeout(4000)
            toasts = page.locator(".toast-message").all_inner_texts()
            print("== mensagem da Stokki:", toasts)
            print("print:", _print(page, f"{pid}_{etapa}_enviado"))
            depois = situacao_do_pedido(page, pid)
            print(f"== PS-{pid}: situacao depois = {depois}")
            if any("inválida" in t.lower() or "invalida" in t.lower() for t in toasts) or depois == antes:
                print("ATENCAO: a situacao nao mudou ou a Stokki recusou.")
                return 1
            return 0
        finally:
            browser.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Avanca pedido Cross Aberto -> Aguardando Transportador na Stokki.")
    ap.add_argument("--pedido", required=True, help="PS-12345")
    ap.add_argument("--etapa", required=True, choices=ETAPAS)
    ap.add_argument("--confirmar", action="store_true", help="envia o modal (sem isso, so mostra)")
    a = ap.parse_args()
    return executar(a.pedido, a.etapa, a.confirmar)


if __name__ == "__main__":
    sys.exit(main())
