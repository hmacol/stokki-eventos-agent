# -*- coding: utf-8 -*-
"""
stokki/conferir_cross_diario.py -- rotina diaria (18:06, timer stokki-conferir-cross-diario):
leva de "Aberto" a "Aguardando Transportador" os pedidos Cross de TODOS os embarcadores com
fluxo cross no cadastro (interno.fluxos), menos os de EXCLUIDOS (Jersey Vale, 79; Hugo 24/11).

Usa stokki/conferir_cross.py pedido a pedido (entra na fila da conta provider). A guarda
principal e o SKU: so avanca pedido cujo unico item e o XD do embarcador; pedido de
armazenagem (itens reais) de embarcador que tambem tem cross fica como esta. Falha num
pedido nao trava os demais; no fim sai com codigo 1 se algum falhou (OnFailure avisa).

    venv/bin/python stokki/conferir_cross_diario.py              # so mostra o plano
    venv/bin/python stokki/conferir_cross_diario.py --confirmar
"""
import argparse
import sqlite3
import sys
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import yaml  # noqa: E402

from nucleo.fluxo_embarcador import fluxos_do_embarcador  # noqa: E402
from stokki import conferir_cross as cc  # noqa: E402
from stokki import pedidos as stokki_pedidos  # noqa: E402
from stokki.auth import StokkiSession  # noqa: E402

EXCLUIDOS = {79}          # Jersey Vale Agroindustrial
STATUS_ABERTO = "Open"


def _embarcadores_cross() -> list[dict]:
    conn = sqlite3.connect(f"file:{_RAIZ / 'dados' / 'dados.db'}?mode=ro", uri=True)
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT stkkc_id FROM interno WHERE stkkc_id IS NOT NULL AND fluxos LIKE '%cross%' "
            "AND fluxos NOT LIKE '%full_cross%' ORDER BY stkkc_id")]
        return [fluxos_do_embarcador(conn, stkkc=i) for i in ids if i not in EXCLUIDOS]
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross diario: Aberto -> Aguardando Transportador.")
    ap.add_argument("--confirmar", action="store_true", help="executa (sem isso so lista)")
    a = ap.parse_args()
    config = yaml.safe_load((_RAIZ / "config.yaml").read_text(encoding="utf-8")) or {}
    sessao = StokkiSession(config)
    falhas = 0
    for emb in _embarcadores_cross():
        if not emb["codigo_xd"]:
            print(f"-- {emb['apelido']} (stkkc {emb['stkkc']}): sem XD ({emb['erro_xd']}) -- pulado")
            continue
        linhas = list(stokki_pedidos.iterar_todos_pedidos(sessao, status=STATUS_ABERTO, cliente=str(emb["stkkc"])))
        pids = sorted(i for i in (stokki_pedidos.extrair_id_da_linha(l) for l in linhas) if i)
        print(f"-- {emb['apelido']} (stkkc {emb['stkkc']}, XD {emb['codigo_xd']}): {len(pids)} aberto(s)")
        # sem texto de cliente na guarda: o SKU XD do embarcador e a trava
        cc.PERFIL.update(sku=emb["codigo_xd"], linha="", conferencia="")
        for pid in pids:
            if cc.executar([pid], a.confirmar) != 0:
                falhas += 1
    print(f"fim: {falhas} pedido(s) com falha")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
