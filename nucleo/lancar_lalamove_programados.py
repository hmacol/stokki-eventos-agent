# -*- coding: utf-8 -*-
"""
nucleo/lancar_lalamove_programados.py

Lança, no horário escolhido no card do /planejamento (campo "Lançar às",
coluna rascunhos_rota.lalamove_lancar_em), a corrida IMEDIATA na
Lalamove das rotas do motorista virtual LALAMOVE já enviadas à VUUPT --
ver lalamove_integracao.lancar_programados. Pedido do Hugo, 03/09: o
horário do lançamento é nosso, sem usar o agendamento (scheduleAt) da
própria Lalamove.

COMO USAR (timer systemd na VPS a cada minuto, infra/stokki-lancar-lalamove.*):
    python nucleo/lancar_lalamove_programados.py
    python nucleo/lancar_lalamove_programados.py --modo-teste   # mostra o que lançaria, não cria corrida
"""
import argparse
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("nucleo.lancar_lalamove_programados")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lança na Lalamove as corridas com horário programado no /planejamento.")
    parser.add_argument("--modo-teste", action="store_true", help="mostra o que lançaria, sem criar corrida nem gravar")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "lancar_lalamove_programados.log", encoding="utf-8")],
    )

    from lalamove_integracao import _carregar_config, cfg_lalamove, lancar_programados
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente no config.yaml.")
        return 1
    if not cfg_lalamove(config).get("api_key"):
        logger.info("lalamove.api_key vazio no config.yaml -- nada a lançar.")
        return 0

    stats = lancar_programados(token, config, modo_teste=args.modo_teste)
    # Roda a cada minuto: só faz barulho no log quando teve o que fazer.
    (logger.info if stats["pendentes"] else logger.debug)(f"Concluído: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
