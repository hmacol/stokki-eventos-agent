# -*- coding: utf-8 -*-
"""
marcar_clientes_boleto.py

Sugere os pares embarcador+cliente que pagam com boleto (regras/
clientes_boleto.py) -- pedido do Hugo, 06/10/2026. Par novo vira
PENDENTE e o time decide em /painel/clientes-boleto; o link chega no
WhatsApp interno quando entra pendente novo.

Roda na sequência das 18h (infra/sequencia_incremento.sh), antes do
aviso_boleto_faltando.py.

COMO USAR:
    py -3.11 marcar_clientes_boleto.py
    py -3.11 marcar_clientes_boleto.py --modo-teste
"""
import argparse
import logging
import sys
import time
from pathlib import Path

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import yaml

import notificar_whatsapp
from notificar_execucao_agente import notificar_execucao
from regras import clientes_boleto as cb

logger = logging.getLogger("marcar_clientes_boleto")
TITULO = "Clientes com boleto"
URL_PAINEL_PADRAO = "https://app.freshhub.com.br/painel"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def link_tela(config: dict) -> str:
    base = ((config or {}).get("painel_agentes") or {}).get("url_base") or URL_PAINEL_PADRAO
    return base.rstrip("/") + "/clientes-boleto"


def main(modo_teste: bool = False) -> dict:
    inicio = time.time()
    config = _carregar_config()
    resumo, r = {}, {"novos": 0, "total_pendentes": 0}
    try:
        res = cb.sugerir(modo_teste=modo_teste)
        r["novos"] = len(res["novos"])
        r["total_pendentes"] = len(cb.listar(cb.PENDENTE)) + (r["novos"] if modo_teste else 0)
        for sid, doc, nome in res["novos"]:
            logger.info(f"  {'[Teste] ' if modo_teste else ''}{nome or '(sem nome)'} ({doc}) remetente {sid}: "
                        f"sugerido como 'tem boleto'.")
        if r["novos"]:
            resumo[TITULO] = {"status": "ok",
                              "detalhe": f"{r['novos']} cliente(s) sugerido(s) como 'tem boleto' "
                                         f"({r['total_pendentes']} aguardando). Decidir em {link_tela(config)}"}
    except Exception as e:
        logger.exception(f"Erro ao sugerir clientes com boleto: {e}")
        resumo[TITULO] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    if resumo:
        try:
            notificar_execucao(resumo, duracao, modo_teste, config)
        except Exception as e:
            logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")
    if r["novos"]:
        notificar_whatsapp.avisar_clientes_boleto(r["novos"], r["total_pendentes"], link_tela(config), config,
                                                  modo_teste=modo_teste)
    return r


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    parser = argparse.ArgumentParser(description="Sugere clientes que pagam com boleto (fila de decisão no painel)")
    parser.add_argument("--modo-teste", action="store_true", help="Só lista, sem gravar nem avisar o grupo")
    main(modo_teste=parser.parse_args().modo_teste)
