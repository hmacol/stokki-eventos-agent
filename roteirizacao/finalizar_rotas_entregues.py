# -*- coding: utf-8 -*-
"""
finalizar_rotas_entregues.py

11/11 (Hugo): finaliza na Vuupt, sem ninguem abrir o app, a rota que o
motorista INICIOU, entregou tudo e esqueceu de encerrar. Sem isso a rota
fica "iniciada" pra sempre.

So entra rota:
  - com start_at nos ultimos DIAS_ATRAS dias (ate hoje);
  - status "started" (iniciada pelo motorista no app);
  - com todos os servicos fechados (done ou cancelado) e pelo menos um done.

Rota que o motorista nunca iniciou (assigned / not_started) NAO da pra
fechar pela conta: PUT /finish devolve 409 "orders.cannot_update" e o
/start so aceita o proprio motorista (403). Provado em 11/11; essas ficam
de fora e so aparecem no log.

16/11 (Hugo): a excecao e a rota da LALAMOVE (agente virtual, nunca inicia).
Essa fecha pela troca de agente provada em 11/11: passa a rota pro agente do
token, accept + start, DEVOLVE pra Lalamove e finish. Se a devolucao falhar,
para as trocas da rodada e sai com erro (alerta). So a Lalamove: com motorista
de verdade nao foi medido se a troca manda aviso no app.

Uso: python finalizar_rotas_entregues.py [--modo-teste]
"""
import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import yaml

from rotas_client import buscar_rota, comando_rota, finalizar_rota, listar_rotas, trocar_agente_rota

logger = logging.getLogger("finalizar_rotas_entregues")

DIAS_ATRAS = 14
STATUS_SERVICO_CANCELADO = {"canceled", "cancelled"}
STATUS_ROTA_NAO_INICIADA = ("assigned", "not_started")
AGENTE_LALAMOVE = 50258
AGENTE_DO_TOKEN = 28438   # agente "TESTE", dono do token da conta


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _servicos(rota: dict) -> list[dict]:
    servicos = rota.get("services")
    if isinstance(servicos, dict):
        servicos = servicos.get("data")
    return servicos or []


def _tudo_entregue(rota: dict) -> bool:
    status = [s.get("status") for s in _servicos(rota)]
    return "done" in status and all(s == "done" or s in STATUS_SERVICO_CANCELADO for s in status)


def rotas_para_finalizar(rotas: list[dict]) -> list[dict]:
    return [r for r in rotas if r.get("status") == "started" and _tudo_entregue(r)]


def rotas_lalamove_para_finalizar(rotas: list[dict]) -> list[dict]:
    return [r for r in rotas if r.get("status") in STATUS_ROTA_NAO_INICIADA
            and r.get("agent_id") == AGENTE_LALAMOVE and _tudo_entregue(r)]


class DevolucaoFalhou(Exception):
    """A rota ficou no agente do token: ninguem mais troca nada nesta rodada."""


def _ler_rota(token: str, route_id: int) -> dict:
    corpo = buscar_rota(token, route_id, include=["services"])
    return corpo.get("route", corpo)


def finalizar_rota_lalamove(token: str, route_id: int) -> bool:
    """Devolve False (sem mexer) se a rota mudou desde a listagem."""
    if not rotas_lalamove_para_finalizar([_ler_rota(token, route_id)]):
        return False
    trocar_agente_rota(token, route_id, AGENTE_DO_TOKEN)
    erro = None
    try:
        try:
            comando_rota(token, route_id, "accept")
        except Exception as e:   # ja aceita: o start decide
            logger.info(f"rota {route_id}: accept recusado ({e})")
        comando_rota(token, route_id, "start")
    except Exception as e:
        erro = e
    try:
        trocar_agente_rota(token, route_id, AGENTE_LALAMOVE)
        voltou = _ler_rota(token, route_id).get("agent_id") == AGENTE_LALAMOVE
    except Exception as e:
        raise DevolucaoFalhou(f"rota {route_id} NAO voltou pra Lalamove: {e}") from e
    if not voltou:
        raise DevolucaoFalhou(f"rota {route_id} NAO voltou pra Lalamove")
    if erro:
        raise erro
    finalizar_rota(token, route_id)
    return True


def main(modo_teste: bool = False, hoje: date | None = None) -> int:
    prefixo = "[MODO TESTE] " if modo_teste else ""
    hoje = hoje or date.today()
    token = _carregar_config().get("vuupt_api", {}).get("token", "")
    if not token:
        raise RuntimeError("config.yaml sem vuupt_api.token.")

    # start_at vem em UTC: o "ate amanha" cobre a rota de hoje a noite.
    filtro = [
        {"field": "start_at", "operator": "gte", "value": f"{hoje - timedelta(days=DIAS_ATRAS):%Y-%m-%d} 00:00:00"},
        {"field": "start_at", "operator": "lt", "value": f"{hoje + timedelta(days=1):%Y-%m-%d} 00:00:00"},
    ]
    rotas = listar_rotas(token, include=["services"], filtro=filtro)

    lalamove = rotas_lalamove_para_finalizar(rotas)
    presas = [r for r in rotas if r.get("status") in STATUS_ROTA_NAO_INICIADA and _tudo_entregue(r)
              and r not in lalamove]
    if presas:
        logger.info(f"{len(presas)} rota(s) com tudo entregue que o motorista nunca iniciou (a Vuupt nao deixa "
                    f"fechar pela conta): {', '.join(str(r.get('id')) for r in presas)}")

    falhas = 0
    alvo = rotas_para_finalizar(rotas)
    for rota in alvo:
        descricao = f"{rota.get('id')} {rota.get('name')} ({len(_servicos(rota))} servico(s))"
        if modo_teste:
            logger.info(f"{prefixo}finalizaria {descricao}")
            continue
        try:
            finalizar_rota(token, rota["id"])
            logger.info(f"finalizada {descricao}")
        except Exception as e:
            falhas += 1
            logger.error(f"falha ao finalizar {descricao}: {e}")
    for rota in lalamove:
        descricao = f"{rota.get('id')} {rota.get('name')} ({len(_servicos(rota))} servico(s))"
        if modo_teste:
            logger.info(f"{prefixo}finalizaria (Lalamove, pela troca) {descricao}")
            continue
        try:
            if finalizar_rota_lalamove(token, rota["id"]):
                logger.info(f"finalizada (Lalamove, pela troca) {descricao}")
            else:
                logger.info(f"pulada, mudou desde a listagem: {descricao}")
        except DevolucaoFalhou as e:
            falhas += 1
            logger.error(f"PAREI as trocas: {e}")
            break
        except Exception as e:
            falhas += 1
            logger.error(f"falha ao finalizar (Lalamove) {descricao}: {e}")
    logger.info(f"{prefixo}{len(alvo)} rota(s) iniciada(s) e {len(lalamove)} da Lalamove com tudo entregue, "
                f"{falhas} falha(s).")
    return falhas


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Finaliza na Vuupt as rotas iniciadas com tudo entregue.")
    parser.add_argument("--modo-teste", action="store_true", help="So lista; nao finaliza nada na Vuupt.")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(_RAIZ_LOCAL / "dados" / "finalizar_rotas_entregues.log", encoding="utf-8"),
        ],
    )
    sys.exit(1 if main(modo_teste=args.modo_teste) else 0)
