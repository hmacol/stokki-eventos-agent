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

22/11 (Hugo): o mesmo vale pra rota operada pelo NOSSO app (nucleo_rotas.operada_app=1),
que fica "not_started" na Vuupt. Pela troca, devolvendo agente E veiculo e conferindo os
dois. Atras da chave finalizar_rotas_entregues.fechar_app_nao_iniciada do config (desligada
por padrao). Nao medido: se a troca manda aviso no app Vuupt do motorista.

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
MAX_TROCAS_POR_RODADA = 15


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


def rotas_app_para_finalizar(rotas: list[dict], ids_app: set[int]) -> list[dict]:
    """Rota do nosso app (id no nucleo com operada_app=1), com motorista de verdade."""
    return [r for r in rotas if r.get("status") in STATUS_ROTA_NAO_INICIADA
            and r.get("id") in ids_app
            and r.get("agent_id") not in (None, 0, AGENTE_LALAMOVE, AGENTE_DO_TOKEN)
            and _tudo_entregue(r)]


def _ids_rotas_do_app() -> set[int]:
    from nucleo import banco
    conn = banco.conectar()
    try:
        return {int(l[0]) for l in conn.execute(
            "SELECT vuupt_route_id FROM nucleo_rotas WHERE operada_app = 1 AND vuupt_route_id IS NOT NULL")}
    finally:
        conn.close()


class DevolucaoFalhou(Exception):
    """A rota ficou no agente do token: ninguem mais troca nada nesta rodada."""


def _ler_rota(token: str, route_id: int) -> dict:
    corpo = buscar_rota(token, route_id, include=["services"])
    return corpo.get("route", corpo)


def finalizar_rota_lalamove(token: str, route_id: int) -> bool:
    return finalizar_rota_pela_troca(token, route_id, rotas_lalamove_para_finalizar)


def finalizar_rota_pela_troca(token: str, route_id: int, elegivel) -> bool:
    """Passa a rota pro agente do token, accept + start, DEVOLVE agente e veiculo originais e
    finish. `elegivel(rotas)` filtra a rota relida na hora. Devolve False (sem mexer) se a rota
    mudou desde a listagem."""
    atual = _ler_rota(token, route_id)
    if not elegivel([atual]):
        return False
    original, veiculo = atual["agent_id"], atual.get("vehicle_id")
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
        trocar_agente_rota(token, route_id, original, veiculo)
        depois = _ler_rota(token, route_id)
        voltou = depois.get("agent_id") == original and (not veiculo or depois.get("vehicle_id") == veiculo)
    except Exception as e:
        raise DevolucaoFalhou(f"rota {route_id} NAO voltou pro agente {original}: {e}") from e
    if not voltou:
        raise DevolucaoFalhou(f"rota {route_id} NAO voltou pro agente {original} / veiculo {veiculo}")
    if erro:
        raise erro
    finalizar_rota(token, route_id)
    return True


def main(modo_teste: bool = False, hoje: date | None = None) -> int:
    prefixo = "[MODO TESTE] " if modo_teste else ""
    hoje = hoje or date.today()
    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    chave_app = bool((config.get("finalizar_rotas_entregues") or {}).get("fechar_app_nao_iniciada"))
    if not token:
        raise RuntimeError("config.yaml sem vuupt_api.token.")

    # start_at vem em UTC: o "ate amanha" cobre a rota de hoje a noite.
    filtro = [
        {"field": "start_at", "operator": "gte", "value": f"{hoje - timedelta(days=DIAS_ATRAS):%Y-%m-%d} 00:00:00"},
        {"field": "start_at", "operator": "lt", "value": f"{hoje + timedelta(days=1):%Y-%m-%d} 00:00:00"},
    ]
    rotas = listar_rotas(token, include=["services"], filtro=filtro)

    lalamove = rotas_lalamove_para_finalizar(rotas)
    app = rotas_app_para_finalizar(rotas, _ids_rotas_do_app()) if (chave_app or modo_teste) else []
    if modo_teste and not chave_app and app:
        logger.info(f"{prefixo}chave fechar_app_nao_iniciada DESLIGADA: em producao estas {len(app)} so ficariam no log")
    presas = [r for r in rotas if r.get("status") in STATUS_ROTA_NAO_INICIADA and _tudo_entregue(r)
              and r not in lalamove and r not in app]
    if app and not modo_teste and len(app) > MAX_TROCAS_POR_RODADA:
        logger.info(f"{len(app)} rota(s) do app: so as {MAX_TROCAS_POR_RODADA} primeiras nesta rodada")
        presas += app[MAX_TROCAS_POR_RODADA:]
        app = app[:MAX_TROCAS_POR_RODADA]
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
    ids_app = {r["id"] for r in app}
    for origem, rota in [("Lalamove", r) for r in lalamove] + [("app", r) for r in app]:
        descricao = f"{rota.get('id')} {rota.get('name')} ({len(_servicos(rota))} servico(s))"
        if modo_teste:
            logger.info(f"{prefixo}finalizaria ({origem}, pela troca) {descricao}")
            continue
        elegivel = (lambda rs: rotas_app_para_finalizar(rs, ids_app)) if origem == "app" else rotas_lalamove_para_finalizar
        try:
            if finalizar_rota_pela_troca(token, rota["id"], elegivel):
                logger.info(f"finalizada ({origem}, pela troca) {descricao}")
            else:
                logger.info(f"pulada, mudou desde a listagem: {descricao}")
        except DevolucaoFalhou as e:
            falhas += 1
            logger.error(f"PAREI as trocas: {e}")
            break
        except Exception as e:
            falhas += 1
            logger.error(f"falha ao finalizar ({origem}) {descricao}: {e}")
    logger.info(f"{prefixo}{len(alvo)} rota(s) iniciada(s), {len(lalamove)} da Lalamove e {len(app)} do app "
                f"com tudo entregue, {falhas} falha(s).")
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
