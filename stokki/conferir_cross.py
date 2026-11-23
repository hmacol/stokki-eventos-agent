# -*- coding: utf-8 -*-
"""
stokki/conferir_cross.py -- leva pedido Cross (Quatro Estrelas, 1 item XD) de
"Aberto" ate "Aguardando Transportador" na area de operador da Stokki, pelos
mesmos passos de um operador (provado em 22/11 no PS-44696):

  1. Aberto        -> Em separacao : Selecao de Pedidos por Lista, operador
                                     Recebimentofrio01, "Iniciar separacao por lista"
  2. Em separacao  -> Em Conferencia: Estacao de Conferencia 1, search do pedido e
                                     "Sim" em "finalizar a separacao por lista"
  3. Em Conferencia-> Aguardando Transportador: search abre a conferencia; digitar o
                                     SKU XD + Enter embala o item; "Pular Impressao" nos
                                     dialogos de DANFE; "Fechar e Imprimir" (packaging/store)

Retoma pela situacao atual do pedido (idempotente: Aguardando Transportador e
pulado). Para no primeiro erro. A conferencia fica no nome do usuario da conta
provider (ClaudeIA). Nada e impresso (sempre "Pular Impressao").

Guardas (para se nao bater): cliente Quatro Estrelas na linha/na conferencia,
quantidade de produtos = 1 e SKU na tela = o esperado.

Rodar na VPS, como www-data (entra na fila da conta provider):
    sudo -u www-data venv/bin/python stokki/conferir_cross.py --de 44697 --ate 44713            # so mostra o plano
    sudo -u www-data venv/bin/python stokki/conferir_cross.py --de 44697 --ate 44713 --confirmar
"""
import argparse
import re
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from stokki import avancar_cross as ac  # noqa: E402
from stokki import estacao_impressao as ei  # noqa: E402
from stokki.sessao_uso import na_vez_da_provider  # noqa: E402

URL_CONFERENCIA = f"{ei.BASE_URL}/pt-br/provider/operation/order/packaging"
SKU_XD = "XDQUATROESTRELAS"
CLIENTE_LINHA = "COMERCIO DE CER"          # coluna Cliente na Selecao (truncada)
CLIENTE_CONFERENCIA = "QUATRO ESTRELAS"    # cartao do cliente na conferencia
FINAL = "Aguardando Transportador"


class Parar(Exception):
    pass


def _situacao(page, pid: int) -> str:
    return ac.situacao_do_pedido(page, pid)


def _abrir_conferencia(page, pid: int) -> None:
    """Abre a Estacao de Conferencia 1 e busca o pedido (Enter)."""
    page.goto(URL_CONFERENCIA, wait_until="domcontentloaded", timeout=45_000)
    ei._aguardar_pagina_acalmar(page)
    sel = page.locator("#select_packing_station")
    if sel.count():
        alvo = next((o.strip() for o in sel.locator("option").all_inner_texts() if "Conferência 1" in o), None)
        if alvo:
            sel.select_option(label=alvo)
    busca = page.locator("#input_search:visible").first
    if not busca.count():
        raise Parar("campo de busca da conferencia nao visivel")
    busca.fill(f"#PS-{pid}")
    busca.press("Enter")
    page.wait_for_timeout(5000)


def _fase1_separacao(page, pid: int) -> None:
    ac.confirmar_global.update(abrir_operador=True, confirmar=True, operador=ac.OPERADOR_PADRAO)
    page.goto(ac.URL_PICKING, wait_until="domcontentloaded", timeout=45_000)
    ei._aguardar_pagina_acalmar(page)
    for seletor in ("#btn_option_order", "#btn_order"):
        btn = page.locator(seletor)
        if btn.count() and btn.first.is_visible():
            btn.first.click()
            page.wait_for_timeout(2000)
    campo = page.locator("input:visible:not([type=hidden]):not([type=checkbox])").first
    if not campo.count():
        raise Parar("campo do filtro de pedido unico nao encontrado")
    campo.fill(f"#PS-{pid}")
    campo.press("Enter")
    page.wait_for_timeout(4000)
    linhas = [tr for tr in page.locator("tr").all() if f"PS-{pid}" in (tr.inner_text() or "")]
    if len(linhas) != 1:
        raise Parar(f"filtro devolveu {len(linhas)} linha(s) para PS-{pid}")
    celulas = [" ".join(c.split()) for c in linhas[0].locator("td").all_inner_texts()]
    print("   linha da selecao:", celulas)
    if CLIENTE_LINHA not in " ".join(celulas).upper():
        raise Parar("cliente da linha nao e a Quatro Estrelas")
    if "1" not in celulas[2:3]:
        raise Parar(f"quantidade de produtos != 1 ({celulas[2:3]})")
    page.get_by_text("Selecionar Operador da Tarefa").first.click()
    page.wait_for_timeout(2500)
    if ac._iniciar_separacao_por_lista(page, pid) != 0:
        raise Parar("iniciar separacao por lista nao levou a Em separacao")


def _fase2_finalizar_separacao(page, pid: int) -> None:
    _abrir_conferencia(page, pid)
    sim = page.locator("#btn_picking_list:visible")
    if sim.count() != 1:
        raise Parar("dialogo 'finalizar separacao por lista' nao apareceu")
    sim.click()
    page.wait_for_timeout(8000)


def _fase3_conferir(page, pid: int) -> None:
    _abrir_conferencia(page, pid)
    corpo = page.inner_text("body").upper()
    if CLIENTE_CONFERENCIA not in corpo:
        raise Parar("cartao do cliente na conferencia nao e Quatro Estrelas")
    skus = set(re.findall(r"SKU:\s*([A-Z0-9\-]+)", corpo))
    if skus != {SKU_XD}:
        raise Parar(f"SKUs na conferencia {sorted(skus)} != {{{SKU_XD}}}")
    campo = page.get_by_placeholder(re.compile("c.digo do produto", re.I)).first
    campo.fill(SKU_XD)
    campo.press("Enter")
    clicou_fechar = False
    for _ in range(16):
        page.wait_for_timeout(2500)
        pular = page.locator(".modal.show button:visible", has_text="Pular Impressão")
        if page.locator(".modal.show").count():
            if pular.count():
                pular.first.click()
                continue
            raise Parar("modal inesperado na conferencia: " + page.locator(".modal.show").first.inner_text()[:200].replace("\n", " "))
        if not clicou_fechar:
            fb = page.get_by_role("button", name=re.compile("Fechar e Imprimir")).first
            if fb.count() and fb.is_enabled():
                fb.click()
                clicou_fechar = True
                continue
        break
    # Em alguns pedidos o "Pular Impressao" ja fecha a conferencia (sem "Fechar e Imprimir"):
    # quem decide se deu certo e a situacao final conferida por executar().


@na_vez_da_provider
def executar(pids: list[int], confirmar: bool) -> int:
    config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
    usuario, senha = ei._credenciais_provider(config)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = ei._novo_contexto_disfarcado(browser).new_page()
            ei._login_provider(page, usuario, senha)
            for pid in pids:
                situacao = _situacao(page, pid)
                print(f"== PS-{pid}: situacao = {situacao}")
                if situacao == FINAL:
                    continue
                if situacao not in ("Aberto", "Em separação", "Em Conferência"):
                    print(f"PARADO: situacao inesperada em PS-{pid}: {situacao}")
                    return 1
                if not confirmar:
                    print("   (sem --confirmar: nada enviado)")
                    continue
                try:
                    if situacao == "Aberto":
                        _fase1_separacao(page, pid)
                        situacao = _situacao(page, pid)
                        print(f"   apos fase 1: {situacao}")
                    if situacao == "Em separação":
                        _fase2_finalizar_separacao(page, pid)
                        situacao = _situacao(page, pid)
                        print(f"   apos fase 2: {situacao}")
                    if situacao == "Em Conferência":
                        _fase3_conferir(page, pid)
                        situacao = _situacao(page, pid)
                        print(f"   apos fase 3: {situacao}")
                except Parar as e:
                    print(f"PARADO em PS-{pid}: {e}")
                    print("print:", ac._print(page, f"{pid}_conferir_parado"))
                    return 1
                if situacao != FINAL:
                    print(f"PARADO: PS-{pid} terminou em '{situacao}', esperado '{FINAL}'.")
                    print("print:", ac._print(page, f"{pid}_conferir_final"))
                    return 1
                print(f"   OK PS-{pid} -> {FINAL}")
        finally:
            browser.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross Quatro Estrelas: Aberto -> Aguardando Transportador.")
    ap.add_argument("--de", type=int, required=True, help="id inicial (ex.: 44697)")
    ap.add_argument("--ate", type=int, required=True, help="id final, inclusive")
    ap.add_argument("--confirmar", action="store_true", help="executa (sem isso so mostra a situacao)")
    a = ap.parse_args()
    return executar(list(range(a.de, a.ate + 1)), a.confirmar)


if __name__ == "__main__":
    sys.exit(main())
