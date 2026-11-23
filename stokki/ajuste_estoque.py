# -*- coding: utf-8 -*-
"""
stokki/ajuste_estoque.py -- ajuste de estoque na Stokki por "Ajuste Manual"
(/inventory/adjustment): zerar produto(s) ou o cliente inteiro, igualar o
disponivel a uma contagem e transferir entre enderecos (a Stokki nao tem tela
de transferencia no administrador: vira saida num endereco + entrada no outro).

Endpoints (achados em 07/10 e 27/10/2026, ver memoria
reference_stokki_inventario_ajuste_endpoints):
  GET  inventory/table?client=<id>[&view=address]   saldo (length <= 100, senao 422)
  GET  inventory/adjustment/create/location/select/<armazem>?search=   id do endereco
  GET  inventory/adjustment/product/adjust/<cliente>/<armazem>/<loc>    ajustavel no
       endereco (no maximo 50 produtos por chamada; Virtual nao aparece)
  POST inventory/adjustment/store        cria o ajuste ("Em analise")
  POST inventory/adjustment/confirm/queue + GET confirm/check/<id> ate
       state == "Regularized stock"

O que o ajuste NAO baixa: o endereco Virtual (estoque reservado por pedido,
inclusive de pedido ja cancelado). Falta, Sobra e Ajuste baixam normalmente.

Escrita exige --modo-teste (so mostra o plano) ou --real. Rodar na VPS, como www-data (StokkiSession
entra na fila da conta sozinha):
    sudo -u www-data venv/bin/python stokki/ajuste_estoque.py saldo --cliente "padrao puro" --busca strog
    sudo -u www-data venv/bin/python stokki/ajuste_estoque.py zerar --cliente 23 --produtos 1400 --obs "..." --modo-teste|--real
    sudo -u www-data venv/bin/python stokki/ajuste_estoque.py zerar --cliente 23 --todos --obs "..." --modo-teste|--real
    sudo -u www-data venv/bin/python stokki/ajuste_estoque.py contagem --cliente 100 --arquivo /tmp/alvo.csv \
        --entrada FABENNE-1 --saida-de FABENNE-1 --obs "..." --modo-teste|--real
    sudo -u www-data venv/bin/python stokki/ajuste_estoque.py transferir --cliente 100 --de MANIOCA-1 \
        --para FABENNE-1 [--produtos 2279 2282] --obs "..." --modo-teste|--real
"""
import argparse
import re
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from bs4 import BeautifulSoup  # noqa: E402

from stokki import contas  # noqa: E402
from stokki.auth import BASE_URL, StokkiSession  # noqa: E402
from stokki.pedidos import listar_embarcadores  # noqa: E402

ADM = f"{BASE_URL}/pt-br/administrator"
ARMAZEM = 1
CAUSA = "Quantitative conference"
HTML = {"X-Requested-With": "", "Accept": "text/html"}
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/javascript, */*; q=0.01"}
COLS_SALDO = ["selection", "address", "zone", "type", "product", "stock", "committed", "available", "actions"]
COLS_AJUSTES = ["id", "date", "requester", "reference", "client", "provider", "type", "state", "action"]


def limpa(v) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(v or "")).split())


def numero(texto) -> float:
    """'1.234,5' -> 1234.5; '14 (Em andamento)' -> 14."""
    m = re.match(r"-?\d+(?:[.,]\d+)?", limpa(texto).replace(".", ""))
    return float(m.group(0).replace(",", ".")) if m else 0.0


def _params_tabela(extra: dict, colunas: list) -> dict:
    p = {"draw": 1, "start": 0, "length": 100, "search[value]": "", "search[regex]": "false",
         "order[0][column]": 1, "order[0][dir]": "asc"}
    for c, nome in enumerate(colunas):
        p.update({f"columns[{c}][data]": nome, f"columns[{c}][name]": nome, f"columns[{c}][searchable]": "true",
                  f"columns[{c}][orderable]": "true", f"columns[{c}][search][value]": "",
                  f"columns[{c}][search][regex]": "false"})
    p.update(extra)
    return p


def _qtd(row, chave) -> float:
    v = row.get(chave)
    return numero(v.get("text") if isinstance(v, dict) else v)


# ── leitura ───────────────────────────────────────────────────────────────────

def achar_cliente(sess, termo: str) -> list[tuple[int, str]]:
    """[(id, nome)] do embarcador com esse id (termo numerico) ou cujo nome contem `termo`."""
    lista = listar_embarcadores(sess)
    if isinstance(lista, dict):
        lista = next((v for v in lista.values() if isinstance(v, list)), [])
    termo = str(termo).strip()
    out = []
    for e in lista:
        if not isinstance(e, dict):
            continue
        nome = e.get("select_label") or e.get("name") or ""
        if (str(e.get("id")) == termo) if termo.isdigit() else (termo.lower() in nome.lower()):
            out.append((int(e["id"]), nome))
    return out


def _tabela_saldo(sess, cliente: int, view: str | None) -> list[dict]:
    linhas, start = [], 0
    while True:
        extra = {"client": cliente, "warehouse": "", "state": "", "classification": "", "marker": "", "chip": "",
                 "input_search": "", "start": start}
        if view:
            extra["view"] = view
        r = sess.get(f"{ADM}/inventory/table", params=_params_tabela(extra, COLS_SALDO),
                     headers={**AJAX, "Referer": f"{ADM}/inventory"})
        r.raise_for_status()
        j = r.json()
        rows = j.get("aaData", [])
        linhas += rows
        start += 100
        if not rows or start >= int(j.get("iTotalDisplayRecords") or 0):
            return linhas


def saldo_por_produto(sess, cliente: int) -> dict[int, dict]:
    out = {}
    for row in _tabela_saldo(sess, cliente, None):
        out[int(row["product_id"])] = {
            "sku": row.get("sku") or "", "nome": limpa((row.get("product") or {}).get("text")),
            "estoque": _qtd(row, "stock"), "comprometido": _qtd(row, "committed"), "disponivel": _qtd(row, "available"),
        }
    return out


def saldo_por_endereco(sess, cliente: int) -> list[dict]:
    return [{"endereco": row.get("address"), "product_id": int(row["product_id"]), "estoque": _qtd(row, "stock"),
             "comprometido": _qtd(row, "committed"), "disponivel": _qtd(row, "available")}
            for row in _tabela_saldo(sess, cliente, "address")]


def id_do_endereco(sess, codigo: str) -> int | None:
    r = sess.get(f"{ADM}/inventory/adjustment/create/location/select/{ARMAZEM}", params={"search": codigo}, headers=AJAX)
    r.raise_for_status()
    for loc in r.json().get("locations", []):
        if loc.get("code") == codigo:
            return int(loc["id"])
    return None


def ajustavel_no_endereco(sess, cliente: int, location_id: int) -> dict[int, float]:
    """{product_id: qtd que um Ajuste de Saida consegue tirar desse endereco}."""
    r = sess.get(f"{ADM}/inventory/adjustment/product/adjust/{cliente}/{ARMAZEM}/{location_id}", headers=AJAX)
    r.raise_for_status()
    return {int(p["id"]): numero(p.get("quantity_available")) for p in r.json().get("products", [])}


# ── planos (puros) ────────────────────────────────────────────────────────────

def plano_zerar(linhas: list[dict], ajustavel: dict[str, dict[int, float]], produtos: set | None = None):
    """linhas = saldo_por_endereco; ajustavel = {endereco: {product_id: qtd}}.
    -> (saidas {endereco: [(pid, qtd)]}, presos [(endereco, pid, qtd, comprometido)])."""
    saidas, presos = {}, []
    for l in linhas:
        if l["estoque"] <= 0 or (produtos is not None and l["product_id"] not in produtos):
            continue
        q = min(l["estoque"], ajustavel.get(l["endereco"], {}).get(l["product_id"], 0))
        if q > 0:
            saidas.setdefault(l["endereco"], []).append((l["product_id"], q))
        if l["estoque"] - q > 0:
            presos.append((l["endereco"], l["product_id"], l["estoque"] - q, l["comprometido"]))
    return saidas, presos


def plano_contagem(saldo: dict[int, dict], alvo: dict[int, float], ordem_saida: list[str],
                   ajustavel: dict[str, dict[int, float]]):
    """Disponivel de cada produto = alvo. Positivo vira entrada; negativo sai dos
    enderecos de `ordem_saida`, nessa ordem.
    -> (entradas [(pid, qtd)], saidas {endereco: [(pid, qtd)]}, pendentes [(pid, falta)])."""
    entradas, saidas, pendentes = [], {}, []
    for pid, quer in alvo.items():
        if pid not in saldo:
            raise ValueError(f"produto {pid} nao esta no saldo do cliente")
        dif = quer - saldo[pid]["disponivel"]
        if dif > 0:
            entradas.append((pid, dif))
        falta = -dif
        for end in ordem_saida:
            if falta <= 0:
                break
            q = min(falta, ajustavel.get(end, {}).get(pid, 0))
            if q > 0:
                saidas.setdefault(end, []).append((pid, q))
                falta -= q
        if falta > 0:
            pendentes.append((pid, falta))
    return entradas, saidas, pendentes


def ler_alvos(texto: str) -> dict[int, float]:
    """Linhas 'product_id;alvo' (ou com virgula/tab). '#' comenta; cabecalho sem numero e ignorado."""
    out = {}
    for linha in texto.splitlines():
        linha = linha.split("#")[0].strip()
        partes = [p.strip() for p in re.split(r"[;,\t]", linha) if p.strip()]
        if len(partes) >= 2 and partes[0].isdigit():
            out[int(partes[0])] = numero(partes[1])
    return out


# ── escrita ───────────────────────────────────────────────────────────────────

def _token_e_usuario(sess, tipo: str) -> tuple[str, str]:
    r = sess.get(f"{ADM}/inventory/adjustment/create/{tipo}", headers=HTML)
    r.raise_for_status()
    form = BeautifulSoup(r.text, "html.parser").find("form", id="form_order")
    return form.find("input", {"name": "_token"})["value"], form.find("input", {"name": "user_id"})["value"]


def ajustes_recentes(sess, cliente: int) -> list[dict]:
    extra = {"type": "", "state": "", "client": cliente, "provider": "", "order[0][column]": 0, "order[0][dir]": "desc"}
    r = sess.get(f"{ADM}/inventory/adjustment/table", params=_params_tabela(extra, COLS_AJUSTES), headers=AJAX)
    r.raise_for_status()
    out = []
    for row in r.json().get("aaData", []):
        m = re.search(r"stkkadj-(\d+)", str(row.get("id")))
        btn = BeautifulSoup(str(row.get("action", "")), "html.parser").find(attrs={"data-type": True})
        out.append({"id": int(m.group(1)) if m else None, "tipo": limpa(row.get("type")), "estado": limpa(row.get("state")),
                    "confirm_type": btn.get("data-type") if btn else None})
    return out


def criar_ajuste(sess, cliente: int, tipo: str, location_id: int, itens: list, observacao: str,
                 dormir=time.sleep) -> dict:
    """tipo = 'incoming' (entrada) ou 'outbound' (saida). Devolve o ajuste novo (ainda nao confirmado)."""
    token, user_id = _token_e_usuario(sess, tipo)
    dados = [("_token", token), ("user_id", user_id), ("requester", "Stokki"), ("type", tipo),
             ("client_id", str(cliente)), ("warehouse_id", str(ARMAZEM)), ("location_id", str(location_id)),
             ("invoice", ""), ("cause", CAUSA), ("observation", observacao)]
    for i, (pid, qtd) in enumerate(itens):
        dados += [(f"product_id[{i}]", str(pid)), (f"quantity[{i}]", str(int(round(qtd))))]
    antes = {a["id"] for a in ajustes_recentes(sess, cliente)}
    r = sess.post(f"{ADM}/inventory/adjustment/store", data=dados,
                  headers={**AJAX, "Referer": f"{ADM}/inventory/adjustment/create/{tipo}"})
    if r.status_code >= 400:
        raise RuntimeError(f"store {tipo} falhou: HTTP {r.status_code} {r.text[:600]}")
    dormir(2)
    novos = [a for a in ajustes_recentes(sess, cliente) if a["id"] not in antes]
    if len(novos) != 1:
        raise RuntimeError(f"esperava 1 ajuste novo, achei {len(novos)}: {novos}")
    return novos[0]


def confirmar(sess, ajuste: dict, dormir=time.sleep, tentativas: int = 60) -> bool:
    token, _ = _token_e_usuario(sess, "outbound")
    tipo = ajuste.get("confirm_type") or ajuste["tipo"]
    r = sess.post(f"{ADM}/inventory/adjustment/confirm/queue",
                  data={"_token": token, "adjustment_id": ajuste["id"], "type": tipo, "invoice": ""},
                  headers={**AJAX, "Referer": f"{ADM}/inventory/adjustment"})
    if r.status_code >= 400:
        raise RuntimeError(f"confirm #{ajuste['id']} falhou: HTTP {r.status_code} {r.text[:600]}")
    for _ in range(tentativas):
        dormir(2)
        rc = sess.get(f"{ADM}/inventory/adjustment/confirm/check/{ajuste['id']}", headers=AJAX)
        try:
            if rc.json().get("state") == "Regularized stock":
                return True
        except ValueError:
            pass
    return False


def aplicar(sess, cliente: int, tipo: str, location_id: int, itens: list, observacao: str) -> int:
    """Cria e confirma um ajuste. Devolve o numero do #stkkadj; levanta erro se nao regularizar."""
    aj = criar_ajuste(sess, cliente, tipo, location_id, itens, observacao)
    rotulo = "ENTRADA" if tipo == "incoming" else "SAIDA"
    if not confirmar(sess, aj):
        raise RuntimeError(f"#stkkadj-{aj['id']} ({rotulo}) criado mas nao regularizou: confira na Stokki")
    print(f"  #stkkadj-{aj['id']} {rotulo} loc {location_id}: regularizado")
    return aj["id"]


# ── CLI ───────────────────────────────────────────────────────────────────────

def _fmt(itens) -> str:
    return ", ".join(f"{pid} {q:+g}" for pid, q in itens)


def _ajustavel(sess, cliente, enderecos) -> tuple[dict, dict]:
    ids, ajust = {}, {}
    for end in enderecos:
        ids[end] = id_do_endereco(sess, end)
        ajust[end] = ajustavel_no_endereco(sess, cliente, ids[end]) if ids[end] else {}
    return ids, ajust


def _resumo_produtos(saldo, pids):
    for pid in sorted(pids):
        s = saldo.get(pid, {})
        print(f"  {pid} {s.get('sku', '')} {s.get('nome', '?')[:50]}: estoque {s.get('estoque', 0):g} "
              f"comprometido {s.get('comprometido', 0):g} disponivel {s.get('disponivel', 0):g}")


def cmd_saldo(sess, cliente, a) -> int:
    saldo = saldo_por_produto(sess, cliente)
    pids = [p for p, s in saldo.items() if not a.busca or a.busca.lower() in (s["nome"] + " " + s["sku"]).lower()]
    _resumo_produtos(saldo, pids)
    if a.busca and pids:
        print("por endereco:")
        for l in saldo_por_endereco(sess, cliente):
            if l["product_id"] in pids and l["estoque"]:
                print(f"  {l['endereco']}: {l['product_id']} estoque {l['estoque']:g} "
                      f"comprometido {l['comprometido']:g} disponivel {l['disponivel']:g}")
    return 0


def cmd_zerar(sess, cliente, a) -> int:
    saldo = saldo_por_produto(sess, cliente)
    produtos = None if a.todos else set(a.produtos)
    if produtos and produtos - set(saldo):
        print(f"produto(s) fora do saldo do cliente: {sorted(produtos - set(saldo))}")
        return 1
    # product/adjust devolve no maximo 50 produtos por endereco: repete enquanto houver o que baixar
    for passada in range(5):
        linhas = saldo_por_endereco(sess, cliente)
        ids, ajust = _ajustavel(sess, cliente, sorted({l["endereco"] for l in linhas if l["estoque"] > 0}))
        saidas, presos = plano_zerar(linhas, ajust, produtos)
        print(f"\nplano (passada {passada + 1}):")
        for end, itens in saidas.items():
            print(f"  SAIDA {end}: {_fmt((p, -q) for p, q in itens)}")
        for end, pid, q, comp in presos:
            print(f"  PRESO {end}: {pid} {q:g} (comprometido {comp:g}) -- ajuste nao baixa")
        if not a.real or not saidas:
            break
        for end, itens in saidas.items():
            aplicar(sess, cliente, "outbound", ids[end], itens, a.obs)
    print("\ndepois:" if a.real else "\nsaldo atual:")
    _resumo_produtos(saldo_por_produto(sess, cliente), produtos or set(saldo))
    return 0


def cmd_contagem(sess, cliente, a) -> int:
    alvo = ler_alvos(Path(a.arquivo).read_text(encoding="utf-8-sig"))
    if not alvo:
        print("arquivo sem linhas 'product_id;alvo'")
        return 1
    saldo = saldo_por_produto(sess, cliente)
    ids, ajust = _ajustavel(sess, cliente, list(dict.fromkeys([a.entrada, *a.saida_de])))
    if None in ids.values():
        print(f"endereco nao encontrado: {[e for e, i in ids.items() if i is None]}")
        return 1
    entradas, saidas, pendentes = plano_contagem(saldo, alvo, a.saida_de, ajust)
    print(f"{'id':>6} {'sku':<14} {'disp':>6} {'alvo':>6} {'ajuste':>7}  nome")
    for pid, quer in alvo.items():
        s = saldo[pid]
        print(f"{pid:>6} {s['sku']:<14} {s['disponivel']:>6g} {quer:>6g} {quer - s['disponivel']:>+7g}  {s['nome'][:45]}")
    print(f"\nENTRADA {a.entrada}: {_fmt(entradas) or '-'}")
    for end, itens in saidas.items():
        print(f"SAIDA {end}: {_fmt((p, -q) for p, q in itens)}")
    for pid, falta in pendentes:
        print(f"PENDENTE {pid}: faltam {falta:g} ajustaveis em {a.saida_de}")
    if not a.real:
        return 0
    if pendentes:
        print("\nABORTADO: ha saida sem saldo ajustavel nos enderecos informados.")
        return 1
    if entradas:
        aplicar(sess, cliente, "incoming", ids[a.entrada], entradas, a.obs)
    for end, itens in saidas.items():
        aplicar(sess, cliente, "outbound", ids[end], itens, a.obs)
    depois = saldo_por_produto(sess, cliente)
    print("\ndepois:")
    for pid, quer in alvo.items():
        ok = "OK" if depois[pid]["disponivel"] == quer else "DIFERENTE"
        print(f"  {pid} disponivel {saldo[pid]['disponivel']:g} -> {depois[pid]['disponivel']:g} (alvo {quer:g}) {ok}")
    return 0


def cmd_transferir(sess, cliente, a) -> int:
    ids, ajust = _ajustavel(sess, cliente, [a.de, a.para])
    if None in ids.values():
        print(f"endereco nao encontrado: {[e for e, i in ids.items() if i is None]}")
        return 1
    itens = [(p, q) for p, q in ajust[a.de].items() if q > 0 and (not a.produtos or p in a.produtos)]
    print(f"TRANSFERIR {a.de} -> {a.para}: {_fmt(itens) or 'nada ajustavel'}")
    if a.produtos and set(a.produtos) - {p for p, _ in itens}:
        print(f"  sem saldo ajustavel em {a.de}: {sorted(set(a.produtos) - {p for p, _ in itens})}")
    if not a.real or not itens:
        return 0
    # saida confirmada antes da entrada: se a saida falhar, nada entra no destino
    aplicar(sess, cliente, "outbound", ids[a.de], itens, a.obs)
    aplicar(sess, cliente, "incoming", ids[a.para], itens, a.obs)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Ajuste de estoque na Stokki (--modo-teste so mostra).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    comum = argparse.ArgumentParser(add_help=False)
    comum.add_argument("--cliente", required=True, help="id do embarcador na Stokki ou parte do nome")
    escrita = argparse.ArgumentParser(add_help=False)
    escrita.add_argument("--obs", required=True, help="observacao gravada no ajuste (quem pediu, data, motivo)")
    modo = escrita.add_mutually_exclusive_group(required=True)
    modo.add_argument("--modo-teste", action="store_true", help="so mostra o plano")
    modo.add_argument("--real", action="store_true", help="cria e confirma os ajustes")

    s = sub.add_parser("saldo", parents=[comum])
    s.add_argument("--busca", help="parte do nome/SKU; mostra tambem por endereco")
    s.add_argument("--modo-teste", action="store_true", help="sem efeito (saldo so le); aceito pelo vps_ops rodar")
    s = sub.add_parser("zerar", parents=[comum, escrita])
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--produtos", type=int, nargs="+", help="product_ids")
    g.add_argument("--todos", action="store_true", help="todo o estoque do cliente")
    s = sub.add_parser("contagem", parents=[comum, escrita])
    s.add_argument("--arquivo", required=True, help="linhas 'product_id;disponivel_alvo'")
    s.add_argument("--entrada", required=True, help="endereco que recebe as sobras (ex.: FABENNE-1)")
    s.add_argument("--saida-de", nargs="+", required=True, help="enderecos de onde sai a falta, em ordem")
    s = sub.add_parser("transferir", parents=[comum, escrita])
    s.add_argument("--de", required=True)
    s.add_argument("--para", required=True)
    s.add_argument("--produtos", type=int, nargs="+", help="so estes (padrao: tudo ajustavel no --de)")
    a = ap.parse_args()

    sess = StokkiSession(contas._ler_config())
    achados = achar_cliente(sess, a.cliente)
    if len(achados) != 1:
        print(f"cliente '{a.cliente}': {len(achados)} encontrado(s) {achados} -- use o id")
        return 1
    cliente, nome = achados[0]
    print(f"== cliente {cliente}: {nome}" + ("" if getattr(a, "real", True) else "  [so mostrando, sem --real]"))
    return {"saldo": cmd_saldo, "zerar": cmd_zerar, "contagem": cmd_contagem, "transferir": cmd_transferir}[a.cmd](
        sess, cliente, a)


if __name__ == "__main__":
    sys.exit(main())
