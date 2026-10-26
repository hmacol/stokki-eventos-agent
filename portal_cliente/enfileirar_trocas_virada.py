# -*- coding: utf-8 -*-
"""
portal_cliente/enfileirar_trocas_virada.py

Virada do fluxo Cross (Hugo, 25/10/2026 -- spec 6.7): os pedidos Cross que JA
existem na Stokki e ainda nao foram expedidos (Aberto, Aguardando
Transportador, Em separacao) entram na fila de troca de NF
(trocas_nf_pendentes), pra a DANFE sair com os produtos reais. O worker do
portal faz a troca; ela e idempotente (pedido que ja tem a NF original
termina FEITA sem POST).

O XML original vem do portal (portal_envios.xml_path) ou do importador por
e-mail (clientes/*/dados/xmls_extraidos/ do agente_importacao_stokki),
casado pelo numero da NF + CNPJ do emitente. Pedido sem original vai pra
lista de troca manual.

Le a Stokki (listagem): exige a trava de sessao e cookies validos -- rode na
VPS, como www-data, nunca local (login novo derruba a sessao de producao):
    sudo -u www-data venv/bin/python portal_cliente/enfileirar_trocas_virada.py --modo-teste
    sudo -u www-data venv/bin/python portal_cliente/enfileirar_trocas_virada.py --executar [--cnpj 66259821000155]
"""
import argparse
import re
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
_AQUI = Path(__file__).parent
for _p in (_RAIZ, _AQUI):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import envio_pedidos as ep  # noqa: E402
import trocas_nf as tq  # noqa: E402
from nucleo.fluxo_embarcador import normalizar_fluxos  # noqa: E402

STATUS_ABERTOS = ("Open", "Waiting for Carrier", "Separating")
DONO_TRAVA = "virada-trocas-nf"
_RE_CHAVE = re.compile(r'Id="NFe(\d{44})"')
_RE_NNF = re.compile(r"<(?:\w+:)?nNF>\s*(\d+)\s*</")
_RE_EMIT = re.compile(r"<(?:\w+:)?emit>.*?<(?:\w+:)?CNPJ>(\d{14})</", re.S)


def listar_abertos(sessao, stkkc: int) -> list[dict]:
    """Pedidos do embarcador nos status nao expedidos: {id_stokki, codigo, numero_nf}.
    numero_nf = referencia da linha (o pedido criado por XML usa a NF)."""
    from stokki import pedidos as sp
    saida, vistos = [], set()
    for status in STATUS_ABERTOS:
        pagina = 0
        while True:
            dados = sp.listar_pedidos(sessao, status=status, cliente=str(stkkc), pagina=pagina, por_pagina=100)
            linhas = dados.get("aaData") or []
            for linha in linhas:
                id_stokki = sp.extrair_id_da_linha(linha)
                if id_stokki and id_stokki not in vistos:
                    vistos.add(id_stokki)
                    saida.append({"id_stokki": id_stokki, "codigo": f"PS-{id_stokki}",
                                  "numero_nf": sp.extrair_referencia_da_linha(linha)})
            if len(linhas) < 100:
                break
            pagina += 1
    return saida


def originais_do_embarcador(conn: sqlite3.Connection, cnpj: str, pastas_importador: list[Path]) -> dict[str, tuple[str, Path]]:
    """{numero_nf: (chave, caminho do XML original)} -- portal primeiro, depois importador."""
    achados: dict[str, tuple[str, Path]] = {}
    for base in pastas_importador:
        for xml in Path(base).glob("*/dados/xmls_extraidos/*.xml"):
            texto = xml.read_text(encoding="utf-8", errors="ignore")
            emit, nnf, chave = _RE_EMIT.search(texto), _RE_NNF.search(texto), _RE_CHAVE.search(texto)
            if emit and nnf and chave and emit.group(1) == cnpj:
                achados[nnf.group(1)] = (chave.group(1), xml)
    for r in conn.execute("SELECT numero_nf, chave_nfe, xml_path FROM portal_envios WHERE cnpj_embarcador = ? "
                          "AND COALESCE(origem, 'xml') = 'xml' AND numero_nf IS NOT NULL AND xml_path IS NOT NULL", (cnpj,)):
        caminho = ep._RAIZ / r[2]
        if caminho.is_file():
            achados[str(r[0])] = (r[1], caminho)
    return achados


def planejar(pedidos: list[dict], originais: dict[str, tuple[str, Path]]) -> tuple[list[dict], list[dict]]:
    fila, manuais = [], []
    for p in pedidos:
        o = originais.get(str(p.get("numero_nf") or ""))
        if o:
            fila.append({**p, "chave_nfe": o[0], "caminho": o[1]})
        else:
            manuais.append(p)
    return fila, manuais


def _embarcadores_cross(conn: sqlite3.Connection, cnpj: str | None = None) -> list[tuple[str, int, str]]:
    colunas = {r[1] for r in conn.execute("PRAGMA table_info(interno)")}
    if "fluxos" not in colunas:
        return []
    sql = "SELECT cnpj_embarcador, stkkc_id, COALESCE(apelido, nome_remetente, cnpj_embarcador), fluxos FROM interno"
    linhas = conn.execute(sql + (" WHERE cnpj_embarcador = ?" if cnpj else ""), ((ep._so_digitos(cnpj),) if cnpj else ())).fetchall()
    return [(r[0], r[1], r[2]) for r in linhas if r[1] is not None and "cross" in normalizar_fluxos(r[3])]


def rodar(conn: sqlite3.Connection, sessao, pastas_importador: list[Path], executar: bool, cnpj: str | None = None) -> dict:
    resumo = {"enfileirados": 0, "manuais": 0, "manuais_lista": []}
    for cnpj_emb, stkkc, nome in _embarcadores_cross(conn, cnpj):
        pedidos = listar_abertos(sessao, stkkc)
        fila, manuais = planejar(pedidos, originais_do_embarcador(conn, cnpj_emb, pastas_importador))
        print(f"{nome} [{stkkc}]: {len(pedidos)} pedido(s) aberto(s), {len(fila)} com XML original, {len(manuais)} sem.")
        for f in fila:
            if executar:
                tq.enfileirar(conn, cnpj_embarcador=cnpj_emb, client_id=str(stkkc), numero_nf=f["numero_nf"],
                              chave_nfe=f["chave_nfe"], caminho_xml_original=str(f["caminho"]), origem="virada",
                              id_stokki=f["id_stokki"], codigo_pedido=f["codigo"], reabrir=True)
            print(f"   {'enfileirado' if executar else 'enfileiraria'}: {f['codigo']} NF {f['numero_nf']}")
        for m in manuais:
            print(f"   TROCA MANUAL (sem XML original): {m['codigo']} referencia {m['numero_nf'] or '-'}")
        resumo["enfileirados"] += len(fila)
        resumo["manuais"] += len(manuais)
        resumo["manuais_lista"] += [{**m, "embarcador": nome} for m in manuais]
    return resumo


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Virada do fluxo Cross: troca de NF dos pedidos ja abertos")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--modo-teste", action="store_true", help="so mostra o que faria (le a Stokki, nao grava a fila)")
    g.add_argument("--executar", action="store_true", help="grava na fila de trocas")
    p.add_argument("--cnpj", help="so este embarcador")
    args = p.parse_args(argv)

    import enviar_stokki as es
    from stokki import sessao_uso
    from stokki.auth import StokkiSession
    config = es.carregar_config()
    if not sessao_uso.adquirir(DONO_TRAVA, ttl_segundos=20 * 60, esperar_segundos=10 * 60, alternativa=True):
        print(f"Stokki ocupada por '{sessao_uso.em_uso()}' -- tente de novo mais tarde.")
        return 2
    conn = ep.conectar()
    try:
        resumo = rodar(conn, StokkiSession(config), [es._caminho_importador(config) / "clientes"],
                       executar=args.executar, cnpj=args.cnpj)
    finally:
        conn.close()
        sessao_uso.liberar(DONO_TRAVA)
    print(f"\n{resumo['enfileirados']} pedido(s) {'enfileirado(s)' if args.executar else 'iriam pra fila'}; "
          f"{resumo['manuais']} pra troca manual.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
