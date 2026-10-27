# -*- coding: utf-8 -*-
"""
stokki/sondar_area_operador.py -- SONDA SO LEITURA da area de operador
(/pt-br/provider/) da Stokki: onde ficam a separacao e a conferencia que o
coletor usa, pra levar um pedido Cross (item XD) de "Aberto" ate
"Aguardando Transportador" (continuacao de sondar_avancar_status.py).

Faz login direto na conta provider (mesmo caminho da Estacao de Impressao,
stokki/estacao_impressao.py) DENTRO da trava da conta provider
(sessao_uso.na_vez_da_provider), entao nao derruba a Estacao. So navega
(GET) e le: menu da area de operador, formularios, botoes e as rotas que
os scripts da pagina chamam. NAO clica em nada nem envia formulario.
Nao imprime cookies, token nem dado de cliente.

Rodar na VPS, como www-data:
    sudo -u www-data venv/bin/python stokki/sondar_area_operador.py [PS-xxxxx]
"""
import re
import sys
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

PALAVRAS = ("separ", "pick", "confer", "check", "pack", "embal", "wave", "onda", "collect", "coletor",
            "order", "pedido", "expedi", "ship", "carrier", "transport", "finaliz", "iniciar", "start")


def _caminho(href: str) -> str:
    return re.sub(r"^https?://[^/]+", "", href or "")


def _resumo_pagina(titulo: str, html: str) -> None:
    soup = BeautifulSoup(html, "html.parser")
    print(f"--- {titulo}")
    for f in soup.find_all("form"):
        campos = [i.get("name") for i in f.find_all(["input", "select", "textarea"]) if i.get("name") and i.get("name") != "_token"]
        print("   form", f.get("id"), f.get("method"), _caminho(f.get("action") or ""), campos)
    vistos = set()
    for a in soup.find_all(["a", "button"]):
        alvo = _caminho(a.get("href") or a.get("data-url") or a.get("data-action") or "")
        texto = " ".join(a.get_text(" ", strip=True).split())[:60]
        if (alvo, texto) in vistos or not (texto or alvo):
            continue
        vistos.add((alvo, texto))
        if "/provider/" in alvo or any(p in (texto + alvo).lower() for p in PALAVRAS):
            print("   ", a.name, a.get("id") or "", alvo, "|", texto)
    rotas = sorted(set(re.findall(r"""['"`](/pt-br/provider/[a-z0-9_/\-{}.]*)['"`]""", html, re.IGNORECASE)))
    for r in rotas:
        print("   rota no script:", r)


def _rotas_dos_scripts(page, url: str) -> None:
    """Le (GET) os .js da propria Stokki carregados pela pagina e o JS inline,
    e lista as rotas /provider/ e os trechos `url: ...`/`$.post(...)` que o
    JavaScript chama -- os formularios da tela nao tem action, quem envia e o JS."""
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    fontes = ["\n".join(s.get_text() for s in soup.find_all("script") if not s.get("src"))]
    for s in soup.find_all("script", src=True):
        src = s["src"]
        if src.startswith("/") or "stokki.com.br" in src:
            alvo = src if src.startswith("http") else ei.BASE_URL + src
            if any(lib in alvo for lib in ("jquery", "bootstrap", "select2", "datatables", "moment", "chart", "sweetalert", "toastr")):
                continue
            try:
                fontes.append(page.request.get(alvo, timeout=30_000).text())
            except Exception:
                pass
    texto = "\n".join(fontes)
    rotas = sorted(set(re.findall(r"""(/pt-br/provider/[A-Za-z0-9_/\-{}.]+|/provider/[A-Za-z0-9_/\-{}.]+)""", texto)))
    chamadas = sorted(set(m.strip()[:140] for m in re.findall(
        r"""((?:url\s*:\s*|\$\.(?:post|get|ajax)\(\s*|fetch\(\s*|axios\.(?:post|get)\(\s*)[^,\n;]{3,120})""", texto)))
    print(f"   [js de {url}] {len(fontes)} fonte(s), {len(rotas)} rota(s), {len(chamadas)} chamada(s)")
    for r in rotas:
        print("   js rota:", r)
    for c in chamadas[:40]:
        print("   js chamada:", c)


@na_vez_da_provider
def sondar(ps: str) -> None:
    config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
    usuario, senha = ei._credenciais_provider(config)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = ei._novo_contexto_disfarcado(browser)
            page = context.new_page()
            ei._login_provider(page, usuario, senha)
            print("== login na area de operador OK")
            # menu: tudo que aponta pra /pt-br/provider/
            soup = BeautifulSoup(page.content(), "html.parser")
            menu = sorted({_caminho(a.get("href")) for a in soup.find_all("a")
                           if a.get("href") and "/provider/" in a.get("href")})
            print("== links da area de operador no menu:")
            for m in menu:
                print("   ", m)
            candidatas = [m for m in menu if any(k in m.lower() for k in ("pick", "separ", "confer", "check", "pack", "collect", "wave"))]
            if "--scripts" in sys.argv:
                candidatas = ["/pt-br/provider/operation/picking/list", "/pt-br/provider/operation/order/packaging",
                              "/pt-br/provider/operation/order", "/pt-br/provider/operation/order/kanban",
                              "/pt-br/provider/operation/order/v2/selection"]
            for url in candidatas[:6]:
                page.goto(ei.BASE_URL + url, wait_until="domcontentloaded", timeout=45_000)
                ei._aguardar_pagina_acalmar(page)
                _resumo_pagina(url, page.content())
                if "--scripts" in sys.argv:
                    _rotas_dos_scripts(page, url)
            if ps and "--scripts" not in sys.argv:
                # busca o pedido do mesmo jeito que a tela de operador busca (GET)
                for url in candidatas[:2]:
                    page.goto(ei.BASE_URL + url + ("&" if "?" in url else "?") + "search=" + ps.replace("PS-", ""),
                              wait_until="domcontentloaded", timeout=45_000)
                    ei._aguardar_pagina_acalmar(page)
                    _resumo_pagina(f"{url} ?search={ps}", page.content())
        finally:
            browser.close()


if __name__ == "__main__":
    sondar(sys.argv[1] if len(sys.argv) > 1 else "")
