# -*- coding: utf-8 -*-
"""
aderencia_plano.py

Aderencia do plano (Hugo, 17/10): quanto do plano entregue pelo
roteirizador chegou ao envio sem a equipe mexer. Compara a foto do plano
(rascunhos_plano_original, gravada por rascunhos_rota.criar_lote_rascunhos)
com as paradas das rotas ENVIADAS da data.

Plano do dia = foto do lote mais recente da data que teve rota enviada
(roteirizar de novo abre lote novo; o "Roteirizar" da selecao acrescenta
no mesmo lote -- vale a foto mais recente de cada pedido).

  mesma_rota  pedido do plano enviado na rota em que nasceu
  movidas     pedido do plano enviado em OUTRA rota (arrastado, fundido)
  fora        pedido do plano que nao foi enviado (adiado, cancelado)
  extras      pedido enviado que nao estava no plano (incremento, manual)
  aderencia   mesma_rota / paradas do plano

Dias anteriores a 17/10 nao tem foto (o lote e editado no lugar).

COMO USAR (da raiz):
    py -3.11 roteirizacao/aderencia_plano.py --de 2026-10-20 --ate 2026-10-24
    py -3.11 roteirizacao/aderencia_plano.py --de ... --ate ... --banco dados/dados_replay.db
"""
import argparse
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BANCO_PADRAO = Path(__file__).parent.parent / "dados" / "dados.db"


def aderencia_do_dia(conn: sqlite3.Connection, dia: str) -> dict | None:
    lote = conn.execute("""
        SELECT r.lote_id FROM rascunhos_rota r
        WHERE r.data_alvo = ? AND r.status = 'ENVIADO'
          AND EXISTS (SELECT 1 FROM rascunhos_plano_original p WHERE p.lote_id = r.lote_id)
        ORDER BY r.id DESC LIMIT 1
    """, (dia,)).fetchone()
    if not lote:
        return None
    plano: dict[int, int] = {}
    for service_id, rascunho_id in conn.execute(
            "SELECT service_id, rascunho_id FROM rascunhos_plano_original WHERE lote_id = ? "
            "ORDER BY gravado_em, rowid", (lote[0],)):
        plano[service_id] = rascunho_id  # a foto mais recente do pedido vence
    enviado: dict[int, int] = {service_id: rascunho_id for service_id, rascunho_id in conn.execute("""
        SELECT p.service_id, p.rascunho_id FROM rascunhos_parada p
        JOIN rascunhos_rota r ON r.id = p.rascunho_id
        WHERE r.data_alvo = ? AND r.status = 'ENVIADO'
    """, (dia,))}
    status = {rid: st for rid, st in conn.execute(
        "SELECT id, status FROM rascunhos_rota WHERE data_alvo = ?", (dia,))}
    mesma = sum(1 for s, rid in plano.items() if enviado.get(s) == rid)
    movidas = sum(1 for s, rid in plano.items() if s in enviado and enviado[s] != rid)
    rotas_plano = set(plano.values())
    return {
        "paradas_plano": len(plano), "mesma_rota": mesma, "movidas": movidas,
        "fora": sum(1 for s in plano if s not in enviado),
        "extras": sum(1 for s in enviado if s not in plano),
        "rotas_plano": len(rotas_plano),
        "rotas_plano_enviadas": sum(1 for rid in rotas_plano if status.get(rid) == "ENVIADO"),
        "aderencia": mesma / len(plano) if plano else 0.0,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--de", required=True)
    parser.add_argument("--ate", required=True)
    parser.add_argument("--banco", default=str(BANCO_PADRAO))
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{args.banco}?mode=ro", uri=True)
    dia, fim = date.fromisoformat(args.de), date.fromisoformat(args.ate)
    print(f"{'dia':10} | aderência | plano | mesma rota | movidas | fora | extras | rotas enviadas/plano")
    while dia <= fim:
        r = aderencia_do_dia(conn, dia.isoformat())
        if r:
            print(f"{dia.isoformat():10} | {r['aderencia']:8.0%} | {r['paradas_plano']:5} | {r['mesma_rota']:10} | "
                  f"{r['movidas']:7} | {r['fora']:4} | {r['extras']:6} | "
                  f"{r['rotas_plano_enviadas']}/{r['rotas_plano']}")
        dia += timedelta(days=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
