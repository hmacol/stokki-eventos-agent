# -*- coding: utf-8 -*-
"""
nucleo/sincronizar_lalamove.py

Acompanha os pedidos Lalamove criados pelo /planejamento (motorista
virtual LALAMOVE, ver lalamove_integracao.py): atualiza status/link no
rascunho e conclui na VUUPT os serviços que a Lalamove já entregou
(comprovante POD por parada, ou pedido COMPLETED).

COMO USAR (timer systemd na VPS a cada 15 min, infra/stokki-sincronizar-lalamove.*):
    python nucleo/sincronizar_lalamove.py
    python nucleo/sincronizar_lalamove.py --modo-teste   # consulta a Lalamove, não grava nem conclui
"""
import argparse
import logging
import sys
from pathlib import Path

_RAIZ = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("nucleo.sincronizar_lalamove")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sincroniza pedidos Lalamove com o planejamento e a VUUPT.")
    parser.add_argument("--modo-teste", action="store_true", help="consulta a Lalamove e mostra, sem gravar nem concluir")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(_RAIZ / "dados" / "sincronizar_lalamove.log", encoding="utf-8")],
    )

    from lalamove_integracao import _carregar_config, cfg_lalamove, sincronizar_pedidos
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        logger.error("vuupt_api.token ausente no config.yaml.")
        return 1
    if not cfg_lalamove(config).get("api_key"):
        logger.info("lalamove.api_key vazio no config.yaml -- nada a sincronizar.")
        return 0

    stats = sincronizar_pedidos(token, config, modo_teste=args.modo_teste)
    logger.info(f"Concluído: {stats}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
