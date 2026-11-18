# -*- coding: utf-8 -*-
"""
Ensaio ponta a ponta no navegador: escritorio monta a onda, galpao separa, distribui,
confere e fecha volumes. Painel de verdade (codigo do worktree) com banco TEMPORARIO e
config de mentira, porta 8199. Nada toca producao, Stokki ou o banco real.

Uso (de dentro de painel_agentes/ do worktree):  py -3.11 ensaio_separacao.py <pasta_dos_prints>
"""
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, ".")
sys.path.insert(0, "..")
import acesso_telas  # noqa: E402
import wms  # noqa: E402
import wms_caixas  # noqa: E402
import wms_pedidos  # noqa: E402
import wms_separacao as sep  # noqa: E402
from test_fila_etiquetas_tela import painel_agentes as pa  # noqa: E402

PRINTS = Path(sys.argv[1])
PORTA = 8199
BASE = f"http://127.0.0.1:{PORTA}"
CFG = {"painel_agentes": {"usuario": "u", "senha": "s"},
       "wms": {"pilotos": [{"id": 22, "nome": "MOMBAK", "caixas": True}]}}

tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
for alvo, valor in ((wms, Path(tmp.name) / "dados.db"), (acesso_telas, Path(tmp.name) / "a.db")):
    mock.patch.object(alvo, "DB_PATH", valor).start()
mock.patch.object(pa, "_carregar_config", return_value=CFG).start()

# ── dados de mentira ──────────────────────────────────────────────────────────
conn = sep.conectar()
for pid, sku, desc, por_caixa in ((1, "MKCA2022", "MOMBAK CAPPUCCINO", 12), (2, "MKMT2022", "MOMBAK MACCHIATO", 12)):
    conn.execute(
        "INSERT INTO wms_produtos (id, stokki_id, sku, descricao, embarcador, embarcador_id, ean, qtd_por_caixa, "
        "unidade, controla_validade, atualizado_em) VALUES (?, ?, ?, ?, 'MOMBAK COMERCIO', 22, ?, ?, 'UN', 0, 'x')",
        (pid, 900 + pid, sku, desc, "789896625916" + str(pid), por_caixa))
wms.criar_area(conn, "C9", "CONTAINER", "Container 9")
for pos in ("C9-E1-N1", "C9-E1-N2"):
    wms.criar_posicao(conn, pos)
conn.commit()
CAPP = wms_caixas.criar_caixas(conn, produto_id=1, lote="L2611", validade=None, qtd_un=None, quantidade=5)["seriais"]
MACC = wms_caixas.criar_caixas(conn, produto_id=2, lote="L2612", validade=None, qtd_un=None, quantidade=2)["seriais"]
for s in CAPP:
    wms_caixas.ativar_caixa(conn, s, "C9-E1-N1")
for s in MACC:
    wms_caixas.ativar_caixa(conn, s, "C9-E1-N2")


def pedido(n, itens, dest, data):
    linhas = [{"linha": i + 1, "sku": sku, "ean_linha": "", "descricao": sku, "qtd_embalagem": q,
               "unidade_do_produto": True} for i, (sku, q) in enumerate(itens)]
    pid = wms_pedidos.registrar_pedido(conn, {
        "id_stokki": n, "codigo_ps": f"PS-{n}", "embarcador": "MOMBAK COMERCIO", "situacao": "Open",
        "embarcador_id": 22, "destinatario": dest, "cidade": "Barueri", "uf": "SP", "endereco_destino": "Al. Rio Negro, 1030",
        "data_expedicao": data, "data_entrega_calc": data}, linhas)
    wms_pedidos.reservar_pedido(conn, pid)
    conn.commit()
    return pid


A = pedido(80001, [("MKCA2022", 24), ("MKMT2022", 12)], "Mercado Bom Preco", "2026-11-24")
B = pedido(80002, [("MKCA2022", 12)], "Emporio Alphaville", "2026-11-24")
C = pedido(80003, [("MKCA2022", 17)], "Cafeteria Central", "2026-11-26")   # 1 caixa + 5 soltas
ANA = wms.criar_operador(conn, "Ana", "1111")
BIA = wms.criar_operador(conn, "Bia", "2222")
conn.close()

pa.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, TEMPLATES_AUTO_RELOAD=True)
threading.Thread(target=lambda: pa.app.run(host="127.0.0.1", port=PORTA, use_reloader=False, threaded=True),
                 daemon=True).start()
time.sleep(1.5)


def cookie(nivel):
    c = pa.app.test_client()
    with c.session_transaction() as s:
        s["nivel_acesso"] = nivel
        s["usuario"] = "ensaio-" + nivel
    ck = c.get_cookie(pa.app.config.get("SESSION_COOKIE_NAME", "session"))
    return {"name": ck.key, "value": ck.value, "url": BASE}


from playwright.sync_api import sync_playwright  # noqa: E402

erros, falhas = [], []


def conferir(cond, texto):
    print(("  ok   " if cond else "  FALHA") + " " + texto)
    if not cond:
        falhas.append(texto)


with sync_playwright() as p:
    nv = p.chromium.launch()

    def contexto(nivel, largura, altura, respostas=None):
        ctx = nv.new_context(viewport={"width": largura, "height": altura})
        ctx.add_cookies([cookie(nivel)])
        ctx.add_init_script("try{localStorage.setItem('tour_visto:ondas','1')}catch(e){}")
        pg = ctx.new_page()
        pg.on("pageerror", lambda e: erros.append(f"[{nivel}] {e}"))
        fila = list(respostas or [])

        def dialogo(d):
            texto = fila.pop(0) if (d.type == "prompt" and fila) else None
            d.accept(texto) if texto is not None else d.accept()
        pg.on("dialog", dialogo)
        pg.respostas = fila
        return pg

    def entrar_operador(pg, op, pin):
        r = pg.evaluate("""async ([id, pin]) => { const r = await fetch('/api/wms/operador/entrar', {method:'POST',
            headers:{'Content-Type':'application/json'}, body: JSON.stringify({operador_id:id, pin:pin})}); return r.status; }""",
                        [op["id"], pin])
        assert r == 200, r
        pg.reload()
        pg.wait_for_timeout(400)

    def bipar(pg, campo, codigo):
        pg.fill(campo, codigo)
        pg.press(campo, "Enter")
        pg.wait_for_timeout(450)

    def resultado(pg):
        el = pg.query_selector("#sep-resultado")
        return (el.get_attribute("class") or "", el.inner_text()) if el else ("", "")

    # 1) Escritorio monta a onda dos dois pedidos do dia 24
    print("ESCRITORIO /ondas")
    esc = contexto("operador", 1366, 800)
    esc.goto(BASE + "/ondas")
    esc.wait_for_timeout(900)
    conferir(esc.locator("#tab-pedidos tbody tr:not(.linha-lotes)").count() == 3, "lista os 3 pedidos da MOMBAK")
    esc.click("#tab-pedidos [data-lotes] >> nth=0")
    esc.wait_for_timeout(500)
    conferir("vai este: vence primeiro" in esc.inner_text("#tab-pedidos .linha-lotes >> nth=0") and "FEFO" not in esc.inner_text("#tab-pedidos"), "painel Lote do cliente mostra o lote que vai sair, sem a sigla")
    esc.screenshot(path=str(PRINTS / "e0_lote_do_cliente.png"), full_page=True)
    esc.click("#tab-pedidos [data-lotes] >> nth=0")
    esc.click("#datas [data-data='2026-11-24']")
    esc.wait_for_timeout(500)
    conferir(esc.locator("#tab-pedidos .marca").count() == 2, "filtro por data de entrega 24/11 deixa 2 pedidos")
    esc.screenshot(path=str(PRINTS / "e1_ondas_montar.png"), full_page=True)
    esc.check("#marcar-todos")
    esc.click("#btn-montar")
    esc.wait_for_timeout(700)
    conferir("Onda 1 montada" in esc.inner_text("#aviso"), "onda 1 montada: " + esc.inner_text("#aviso")[:60])

    # 2) Galpao: Ana separa e distribui
    print("GALPAO /wms (Ana separa)")
    gal = contexto("galpao", 390, 800)
    gal.goto(BASE + "/wms")
    gal.wait_for_timeout(600)
    entrar_operador(gal, ANA, "1111")
    gal.click(".aba[data-aba='separar']")
    gal.wait_for_timeout(700)
    conferir(gal.locator("#sep-ondas [data-onda]").count() == 1, "aba Separar mostra a onda liberada")
    conferir(gal.locator("#sep-soltos [data-solto]").count() == 1, "e o pedido sem onda (PS-80003)")
    gal.screenshot(path=str(PRINTS / "g1_separar_lista.png"))
    gal.click("#sep-ondas [data-onda]")            # confirm "Comecar?" -> aceita
    gal.wait_for_timeout(800)
    conferir(gal.locator("#onda-modos").is_visible(), "onda de 2 pedidos mostra os modos Separar / Distribuir")
    gal.screenshot(path=str(PRINTS / "g2_onda_aberta.png"), full_page=True)
    for s in CAPP[:3]:
        bipar(gal, "#campo-onda", s)
    cls, txt = resultado(gal)
    conferir("ok" in cls and "produto completo" in txt, "3 caixas de cappuccino separadas: " + txt.replace("\n", " | ")[:70])
    bipar(gal, "#campo-onda", CAPP[0])
    cls, txt = resultado(gal)
    conferir("erro" in cls and "ja foi separada" in txt, "caixa repetida e recusada em vermelho")
    gal.screenshot(path=str(PRINTS / "g3_bipe_recusado.png"))
    bipar(gal, "#campo-onda", f"FL|MKMT2022|L2612||{MACC[0]}")   # QR inteiro da etiqueta
    cls, txt = resultado(gal)
    conferir("ok" in cls, "le o QR completo da etiqueta de caixa: " + txt.replace("\n", " | ")[:60])
    gal.click("#onda-modos [data-modo='distribuir']")
    gal.wait_for_timeout(500)
    destinos = []
    for s in CAPP[:3] + [MACC[0]]:
        bipar(gal, "#campo-onda", s)
        destinos.append(resultado(gal)[1].split("\n")[0])
    conferir(destinos == ["PS-80001", "PS-80001", "PS-80002", "PS-80001"], "distribuir diz o pedido de cada caixa: " + str(destinos))
    gal.screenshot(path=str(PRINTS / "g4_distribuir.png"))
    gal.click("#btn-fechar-onda")
    gal.wait_for_timeout(900)
    conferir(gal.locator("#sep-ondas [data-onda]").count() == 0, "separacao fechada: a onda saiu da lista")

    # pedido solto com caixa + parte de caixa (17 UN = 1 caixa + 5)
    gal.click("#sep-soltos [data-solto]")           # confirm -> aceita
    gal.wait_for_timeout(800)
    bipar(gal, "#campo-onda", CAPP[3])
    gal.respostas.append("5")                       # prompt "quantas unidades voce tirou"
    bipar(gal, "#campo-onda", CAPP[4])
    gal.wait_for_timeout(500)
    cls, txt = resultado(gal)
    conferir("ok" in cls and "PS-80003" in txt and "5 UN" in txt, "parte de caixa: pede a quantidade e registra 5 UN: " + txt.replace("\n", " | ")[:70])
    gal.screenshot(path=str(PRINTS / "g5_parte_de_caixa.png"), full_page=True)
    gal.click("#btn-fechar-onda")
    gal.wait_for_timeout(900)

    # Ana tenta conferir o que ela mesma separou
    gal.click("[data-ir='conferir']")
    gal.wait_for_timeout(700)
    conferir(gal.locator("#conf-lista [data-conf]").count() == 3, "3 pedidos esperando conferencia")
    gal.click("#conf-lista [data-conf] >> nth=0")
    gal.wait_for_timeout(600)
    conferir("nao pode conferir" in gal.inner_text("#toast"), "quem separou e barrado na conferencia")

    # 3) Bia confere
    print("GALPAO /wms (Bia confere)")
    bia = contexto("galpao", 390, 800)
    bia.goto(BASE + "/wms")
    bia.wait_for_timeout(600)
    entrar_operador(bia, BIA, "2222")
    bia.click(".aba[data-aba='separar']")
    bia.wait_for_timeout(500)
    bia.click("[data-ir='conferir']")
    bia.wait_for_timeout(700)
    bia.click("#conf-lista [data-conf] >> nth=0")   # PS-80001
    bia.wait_for_timeout(700)
    corpo = bia.inner_text("#app")
    conferir("PS-80001" in corpo and "24" not in corpo.replace("PS-80001", ""), "conferencia cega: a tela nao mostra quantidades do pedido")
    for s in CAPP[:2] + [MACC[0]]:
        bipar(bia, "#campo-conf", s)
    conferir(bia.inner_text("#conf-n") == "3", "contador mostra 3 bipes")
    bipar(bia, "#campo-conf", CAPP[2])
    cls, txt = resultado(bia)
    conferir("erro" in cls and "nao e deste pedido" in txt, "caixa de outro pedido e recusada")
    bia.screenshot(path=str(PRINTS / "c1_conferencia.png"))
    bia.click("#btn-fechar-conf")
    bia.wait_for_timeout(800)
    conferir("CONFERIDO" in bia.inner_text("#app"), "PS-80001 conferido")
    bia.screenshot(path=str(PRINTS / "c2_conferido.png"))
    bia.respostas.append("3")                       # quantos volumes
    bia.click("#btn-ir-volumes")
    bia.wait_for_timeout(700)
    bia.click("#vol-lista [data-vol] >> nth=0")
    bia.wait_for_timeout(900)
    conferir("embalado" in bia.inner_text("#vol-lista").lower(), "volumes fechados: pedido embalado")
    bia.screenshot(path=str(PRINTS / "c3_volumes.png"))

    # PS-80003: caixa inteira + 5 soltas pelo EAN
    bia.click("#btn-voltar")
    bia.wait_for_timeout(400)
    bia.click("[data-ir='conferir']")
    bia.wait_for_timeout(600)
    bia.click("#conf-lista [data-conf]:has-text('PS-80003')")
    bia.wait_for_timeout(600)
    bipar(bia, "#campo-conf", CAPP[3])
    bia.respostas.append("5")
    bipar(bia, "#campo-conf", "7898966259161")
    bia.wait_for_timeout(400)
    bia.click("#btn-fechar-conf")
    bia.wait_for_timeout(800)
    conferir("CONFERIDO" in bia.inner_text("#app"), "PS-80003 (caixa + 5 soltas pelo EAN) conferido")

    # PS-80002: Bia nao bipa nada e fecha -> falta
    bia.click("#btn-outro")
    bia.wait_for_timeout(600)
    bia.click("#conf-lista [data-conf]:has-text('PS-80002')")
    bia.wait_for_timeout(600)
    bia.click("#btn-fechar-conf")
    bia.wait_for_timeout(800)
    conferir("NÃO BATEU" in bia.inner_text("#app"), "PS-80002 sem bipe: nao bateu, pedido travado")
    bia.screenshot(path=str(PRINTS / "c4_falta.png"))

    # 4) Escritorio decide a falta e ve o andamento
    print("ESCRITORIO decide a falta")
    esc.click("#btn-atualizar")
    esc.wait_for_timeout(900)
    conferir(esc.locator("#faltas [data-pedido]").count() == 1, "falta do PS-80002 aparece para o escritorio")
    esc.screenshot(path=str(PRINTS / "e2_ondas_falta.png"), full_page=True)
    esc.click("#faltas [data-decisao='VOLTAR']")
    esc.wait_for_timeout(400)
    conferir("motivo" in esc.inner_text("#aviso").lower(), "sem motivo nao decide")
    esc.fill("#faltas .motivo", "conferente esqueceu de bipar")
    esc.click("#faltas [data-decisao='VOLTAR']")
    esc.wait_for_timeout(900)
    conferir("voltou para separação" in esc.inner_text("#aviso"), "decisao registrada: " + esc.inner_text("#aviso")[:60])
    andamento = esc.inner_text("#tab-andamento tbody")
    conferir("embalado" in andamento and "conferido" in andamento and "separando" in andamento, "andamento mostra embalado, conferido e separando")
    esc.screenshot(path=str(PRINTS / "e3_ondas_andamento.png"), full_page=True)

    # 5) Fila de impressao: etiqueta de volume aparece pra estacao
    est = contexto("expedicao", 1100, 700)
    est.goto(BASE + "/etiquetas/fila")
    est.wait_for_timeout(900)
    conferir("PS-80001 · 3 volume(s)" in est.inner_text("#tab-fila"), "fila de etiquetas mostra os 3 volumes do PS-80001 (10x15)")
    est.screenshot(path=str(PRINTS / "f1_fila.png"))
    nv.close()

# 6) estoque fecha: baixa do PS-80001 e checagem caixas x saldo
conn = sep.conectar()
r = wms_pedidos.baixar_por_expedicao(conn, "PS-80001")
conferir(not r["erros"] and r["baixas"] >= 2, "baixa da expedicao do PS-80001: " + str({k: r[k] for k in ("baixas", "erros")}))
sit = {s: wms_caixas.obter_caixa(conn, s)["situacao"] for s in CAPP + MACC}
conferir([sit[s] for s in CAPP[:2] + [MACC[0]]] == ["EXPEDIDA"] * 3, "as 3 caixas conferidas do pedido sairam como EXPEDIDA")
conferir(wms_caixas.divergencias(conn, 22) == [], "checagem caixas x saldo: 0 divergencia")
conn.close()

print("\nerros de JavaScript:", erros or "nenhum")
print("FALHAS:", falhas or "nenhuma")
sys.exit(1 if (falhas or erros) else 0)
