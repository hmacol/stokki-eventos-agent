# -*- coding: utf-8 -*-
"""
alertar_retornos_atrasados.py

Cobranca dos retornos que nao chegaram ao galpao (Hugo, 23/10/2026): o
motorista pode guardar o insucesso ate a manha seguinte, mas passou das 07h
do proximo dia util sem a descarga confirmada na tela /retornos, a Logistica
precisa acompanhar. Manda UMA mensagem no grupo "Alertas" do /atendimento,
agrupada por motorista; cada retorno e cobrado uma vez so
(retornos_alertas_atraso). Sem retorno atrasado, nao manda nada.

Agendamento (VPS): stokki-retornos-atrasados.timer, seg-sex 07h00. Feriado
e conferido aqui (regras/feriados.py): em feriado o script nao faz nada.

    venv/bin/python alertar_retornos_atrasados.py [--modo-teste] [--data AAAA-MM-DD]

--modo-teste imprime a mensagem e nao posta nem marca nada.
"""
import argparse
import logging
import sys
from datetime import date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_RAIZ = Path(__file__).parent
for _p in (_RAIZ, _RAIZ / "painel_agentes"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import grupos_equipe  # noqa: E402
import retornos  # noqa: E402
from regras.feriados import eh_dia_util  # noqa: E402

logger = logging.getLogger("retornos_atrasados")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--modo-teste", action="store_true", help="so imprime; nao posta nem marca")
    ap.add_argument("--data", type=date.fromisoformat, default=None, help="dia de referencia (padrao hoje)")
    args = ap.parse_args(argv)
    hoje = args.data or date.today()

    if not eh_dia_util(hoje):
        logger.info("%s nao e dia util: nada a cobrar.", hoje)
        return 0

    conn = retornos.conectar()
    try:
        grupos = retornos.pendentes_atrasados(conn, hoje)
        if not grupos:
            logger.info("Nenhum retorno atrasado em %s.", hoje)
            return 0
        texto = retornos.texto_alerta_atraso(grupos)
        ids = [p["parada_id"] for g in grupos for p in g["pedidos"]]
        if args.modo_teste:
            print(f"[MODO TESTE] {len(ids)} retorno(s) seriam cobrados no grupo {retornos.NOME_GRUPO}:\n{texto}")
            return 0
        cg = grupos_equipe.conectar()
        try:
            retornos.postar_alerta(cg, texto)
        finally:
            cg.close()
        retornos.marcar_alertados(conn, ids)
        logger.info("%s retorno(s) atrasado(s) cobrados no grupo %s.", len(ids), retornos.NOME_GRUPO)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    sys.exit(main())
