# -*- coding: utf-8 -*-
"""
batimento/bater.py

Job diario do batimento (DOC_EXECUCAO_CLAUDE_BATIMENTO_PEDIDOS.md): faz a
mesma coleta do medir.py (Stokki Sent/Canceled/abertos desde o corte +
Vuupt done + banco), classifica cada pedido e grava em batimento_pedidos /
batimento_rodadas (batimento/banco.py). Nao escreve na Stokki nem na Vuupt.

Equacao que nao fecha = sai com codigo 1, e o OnFailure do systemd manda o
alerta de falha (stokki-alerta-falha@).

Execute (VPS, como www-data):
  sudo -u www-data venv/bin/python -m batimento.bater
  sudo -u www-data venv/bin/python -m batimento.bater --resumo   # so imprime, nao grava
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent.parent
if str(_RAIZ) not in sys.path:
    sys.path.insert(0, str(_RAIZ))

from batimento import banco, medir  # noqa: E402

logger = logging.getLogger("batimento.bater")

DONO_TRAVA = "batimento"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Batimento diario de pedidos (destino final).")
    ap.add_argument("--data-corte", default=medir.DATA_CORTE_PADRAO.isoformat(), help="YYYY-MM-DD")
    ap.add_argument("--id-minimo", type=int, default=None, help="id da Stokki a partir do qual listar")
    ap.add_argument("--dias-vuupt", type=int, default=30)
    ap.add_argument("--esperar-stokki", type=int, default=1800, help="segundos esperando a vez na Stokki")
    ap.add_argument("--resumo", "--modo-teste", dest="resumo", action="store_true",
                    help="so imprime a equacao, nao grava no banco")
    args = ap.parse_args(argv)

    pedidos, resumo, _contagem, id_minimo = medir.coletar(
        date.fromisoformat(args.data_corte), args.id_minimo, args.dias_vuupt, args.esperar_stokki, DONO_TRAVA)
    medir.logar_resumo(resumo)

    if args.resumo:
        logger.info("--resumo: nada gravado.")
        return 0 if resumo["equacao_fecha"] else 1

    conn = banco.conectar(medir.DB_PATH)
    try:
        r = banco.gravar_rodada(conn, pedidos, resumo, id_minimo)
    finally:
        conn.close()
    logger.info(f"Gravado: {r['novos']} novo(s), {r['mudaram']} mudaram, {r['iguais']} iguais, "
                f"{r['congelados']} ja em DESTINO | rodada: {r['totais']} | equacao "
                f"{'FECHA' if r['fecha'] else 'NAO FECHA'}")
    return 0 if r["fecha"] else 1


if __name__ == "__main__":
    sys.exit(main())
