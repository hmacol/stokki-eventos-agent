# -*- coding: utf-8 -*-
"""
somente_impressao.py

Roda SÓ a etapa de Estação de Impressão (Em espera -> Aguardando
Transportador), separada do executar_tudo.py -- pedido do Hugo, 06/08:
poder disparar a impressão isolada pelo painel de agentes, sem
precisar rodar o pipeline inteiro.

Mesmo padrão dos outros comandos avulsos (expedir_pedidos.py /
"Somente Expedição", pipeline.py / "Somente Importação"): função
principal do módulo já existente, só com CLI e notificação de
execução próprios.

Execute:
  py -3.11 somente_impressao.py
  py -3.11 somente_impressao.py --modo-teste
"""
import argparse
import logging
import sys
import time
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from stokki.estacao_impressao import imprimir_pedidos_pendentes
from notificar_execucao_agente import notificar_execucao

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "somente_impressao.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("somente_impressao")

(_RAIZ / "dados").mkdir(parents=True, exist_ok=True)


def _carregar_config():
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main(modo_teste: bool = False, apenas_ids: set | None = None):
    inicio = time.time()
    logger.info("=" * 60)
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Estação de Impressão (avulsa) iniciada.")
    if apenas_ids:
        logger.info(f"Restrita aos data-ids: {sorted(apenas_ids)}")
    logger.info("=" * 60)

    resumo_etapas = {}
    config = _carregar_config()

    try:
        resultado = imprimir_pedidos_pendentes(config, dry_run=modo_teste, apenas_ids=apenas_ids)
        if modo_teste:
            logger.info(
                f"[MODO TESTE] {resultado['pendentes']} pedido(s) na fila "
                f"da Estação de Impressão (nenhum clique realizado)."
            )
            resumo_etapas["Impressão"] = {
                "status": "ok",
                "detalhe": f"[Teste] {resultado['pendentes']} pedido(s) na fila",
            }
        else:
            logger.info(f"Processados: {resultado['processados']}/{resultado['pendentes']} pedido(s).")
            detalhe = f"{resultado['processados']}/{resultado['pendentes']} processado(s)"
            if resultado.get("ignorados"):
                logger.info(f"{len(resultado['ignorados'])} ignorado(s) de propósito: {resultado['ignorados']}")
                detalhe += f", {len(resultado['ignorados'])} ignorado(s)"
            if resultado["falhas"]:
                logger.warning(f"{len(resultado['falhas'])} falha(s): {resultado['falhas']}")
                detalhe += f", {len(resultado['falhas'])} falha(s)"
            resumo_etapas["Impressão"] = {"status": "ok", "detalhe": detalhe}
    except Exception as e:
        logger.error(f"Erro na Estação de Impressão: {e}")
        resumo_etapas["Impressão"] = {"status": "erro", "detalhe": str(e)}

    elapsed = time.time() - inicio
    logger.info("-" * 60)
    logger.info(f"Estação de Impressão (avulsa) finalizada em {elapsed/60:.1f} minutos.")
    logger.info("=" * 60)

    try:
        notificar_execucao(resumo_etapas, elapsed, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução por e-mail (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Roda só a Estação de Impressão, separada do fluxo completo")
    parser.add_argument("--modo-teste", action="store_true",
                        help="Simula sem clicar em nada -- só conta quantos pedidos estão na fila")
    parser.add_argument("--id", action="append", dest="ids", default=[],
                        help="Processa só este data-id da fila (repetível). Sem --id, processa a fila toda.")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste, apenas_ids=set(args.ids) or None)
