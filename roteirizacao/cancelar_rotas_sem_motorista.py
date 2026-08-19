# -*- coding: utf-8 -*-
"""
cancelar_rotas_sem_motorista.py

Rotina diária de proteção (17h45, ver infra/stokki-cancelar-rotas-sem-
motorista.timer) -- pedido do Hugo, 19/08, depois de achar o PS-36801
"preso" numa rota de véspera sem motorista que nunca foi cancelada.

Por quê: enviar uma rota sem motorista atribuído é fluxo normal e
suportado (rotas_client.criar_rota permite agent_id=None, pra
atribuição manual depois -- 01/08). O problema é quando NINGUÉM atribui
motorista até o fim do dia: a rota fica esquecida na VUUPT pra sempre.
Os pedidos dela aparecem como "not_assigned" no pool (parecem
disponíveis), mas a VUUPT recusa colocá-los em qualquer rota nova
("já está em outra rota", dado desatualizado -- ver
rotas_client.criar_rota_removendo_conflitos) porque o service ainda
referencia o route_id antigo por baixo do status. Sem essa rotina, os
pedidos ficam invisivelmente inutilizáveis até alguém descobrir e
cancelar a rota manualmente.

Elegível pra cancelamento (mesmo critério de segurança de
reprocessar_rotas.py, restrito a HOJE em vez de hoje+futuro):
  - status != "canceled" (já cancelada, nada a fazer)
  - start_at é HOJE (rota de dia futuro pode ainda ganhar motorista
    até lá -- não é candidata ainda)
  - agent_id é None (rota com motorista atribuído NUNCA é tocada,
    mesmo que o motorista ainda não tenha aceitado/iniciado)

cancelar_rota() sempre com services_action="unassign" -- pedidos
voltam pra not_assigned DE VERDADE (limpa o route_id de referência),
liberando pro pool/roteirização normal do dia seguinte. Se a rota
cancelada tinha um rascunho local (enviada pelo painel de
planejamento), reverter_por_vuupt_route_id sincroniza o rascunho de
volta pra RASCUNHO -- sem isso ele ficaria "ENVIADO fantasma" na tela,
apontando pra uma rota que não existe mais.

COMO USAR:
    py -3.11 roteirizacao/cancelar_rotas_sem_motorista.py                # execução normal
    py -3.11 roteirizacao/cancelar_rotas_sem_motorista.py --modo-teste   # só lista, não cancela
"""
import argparse
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

_RAIZ_LOCAL = Path(__file__).parent
_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(_RAIZ_LOCAL))
sys.path.insert(0, str(_RAIZ_PROJETO / "painel_agentes"))
(_RAIZ_LOCAL / "dados").mkdir(parents=True, exist_ok=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ_LOCAL / "dados" / "cancelar_rotas_sem_motorista.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("cancelar_rotas_sem_motorista")

import yaml

from notificar_execucao_agente import notificar_execucao
from rotas_client import listar_rotas, cancelar_rota
from rascunhos_rota import reverter_por_vuupt_route_id

PADRAO_DATA_START_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _data_inicio_rota(rota: dict) -> date | None:
    """Extrai AAAA-MM-DD de start_at. None se ausente/irreconhecível --
    nesse caso a rota NÃO é candidata (não dá pra confirmar que é hoje
    sem essa data)."""
    start_at = rota.get("start_at")
    if not start_at:
        return None
    match = PADRAO_DATA_START_AT.search(str(start_at))
    if not match:
        return None
    ano, mes, dia = match.groups()
    try:
        return date(int(ano), int(mes), int(dia))
    except ValueError:
        return None


def _elegivel(rota: dict, hoje: date) -> tuple[bool, str]:
    """Retorna (elegível, motivo_se_nao)."""
    if rota.get("status") == "canceled":
        return False, "já está cancelada"
    if rota.get("agent_id") is not None:
        return False, f"tem motorista atribuído (agent_id={rota.get('agent_id')})"
    data_inicio = _data_inicio_rota(rota)
    if data_inicio is None:
        return False, "start_at ausente/irreconhecível -- não dá pra confirmar que é hoje"
    if data_inicio != hoje:
        return False, f"start_at ({data_inicio}) não é hoje ({hoje})"
    return True, ""


def main(modo_teste: bool = False):
    inicio = time.time()
    prefixo = "[MODO TESTE] " if modo_teste else ""
    logger.info(f"{prefixo}Cancelamento de rotas sem motorista iniciado.")

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    resumo_etapas = {}

    try:
        if not token:
            raise RuntimeError("config.yaml sem vuupt_api.token.")

        hoje = date.today()
        # filtra por start_at de hoje direto na API -- sem isso
        # listar_rotas traz o HISTÓRICO INTEIRO (milhares de rotas
        # passadas, minutos de paginação) só pra descartar quase tudo
        # depois em Python. Mesmo padrão de incrementar_rotas.py: cai
        # pro histórico completo se o filtro falhar por algum motivo
        # (nunca finge que não há rota nenhuma só porque o filtro deu
        # erro).
        filtro_hoje = [
            {"field": "start_at", "operator": "gte", "value": hoje.strftime("%Y-%m-%d") + " 00:00:00"},
            {"field": "start_at", "operator": "lt", "value": (hoje + timedelta(days=1)).strftime("%Y-%m-%d") + " 00:00:00"},
        ]
        try:
            todas_rotas = listar_rotas(token, filtro=filtro_hoje)
            logger.info(f"{prefixo}{len(todas_rotas)} rota(s) de hoje encontrada(s) via filtro.")
        except Exception as e:
            logger.warning(f"{prefixo}Filtro por start_at falhou ({e}) -- caindo pro histórico completo (mais lento).")
            todas_rotas = listar_rotas(token)
            logger.info(f"{prefixo}{len(todas_rotas)} rota(s) encontrada(s) no total na VUUPT.")

        elegiveis = []
        for rota in todas_rotas:
            ok, motivo = _elegivel(rota, hoje)
            if ok:
                elegiveis.append(rota)
            else:
                logger.debug(f"  ignorada: rota {rota.get('id')} '{rota.get('name')}' -- {motivo}")

        if not elegiveis:
            logger.info(f"{prefixo}Nenhuma rota de hoje sem motorista pra cancelar.")
            resumo_etapas["Cancelamento de rotas sem motorista"] = {
                "status": "ok",
                "detalhe": "Nenhuma rota de hoje sem motorista encontrada.",
            }
        else:
            logger.info(f"{prefixo}{len(elegiveis)} rota(s) de hoje sem motorista, candidata(s) a cancelamento:")
            for rota in elegiveis:
                logger.info(f"{prefixo}  - id={rota.get('id')} '{rota.get('name')}' start_at={rota.get('start_at')} "
                            f"prevision_number_services={rota.get('prevision_number_services')}")

            canceladas, com_erro = [], []
            for rota in elegiveis:
                route_id = rota.get("id")
                nome = rota.get("name")
                if modo_teste:
                    canceladas.append(nome)
                    continue
                try:
                    cancelar_rota(token, route_id, services_action="unassign")
                    rascunho_id = reverter_por_vuupt_route_id(route_id)
                    aviso_rascunho = f" (rascunho local #{rascunho_id} revertido pra RASCUNHO)" if rascunho_id else ""
                    logger.info(f"  OK: rota {route_id} '{nome}' cancelada, pedidos desatribuídos{aviso_rascunho}.")
                    canceladas.append(nome)
                except Exception as e:
                    logger.error(f"  FALHA ao cancelar rota {route_id} '{nome}': {e}")
                    com_erro.append(f"{nome} ({e})")

            detalhe = f"{prefixo}{len(canceladas)} rota(s) sem motorista canceladas: {', '.join(canceladas)}."
            if com_erro:
                detalhe += f" [ALERTA_CANCELAMENTO_ROTA] {len(com_erro)} falha(s): {'; '.join(com_erro)}."
            resumo_etapas["Cancelamento de rotas sem motorista"] = {
                "status": "ok" if not com_erro else "erro",
                "detalhe": detalhe,
            }

    except Exception as e:
        logger.exception(f"Erro no cancelamento de rotas sem motorista: {e}")
        resumo_etapas["Cancelamento de rotas sem motorista"] = {"status": "erro", "detalhe": str(e)}

    duracao = time.time() - inicio
    logger.info(f"{prefixo}Cancelamento de rotas sem motorista finalizado em {duracao:.1f}s.")

    try:
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
    except Exception as e:
        logger.warning(f"Falha ao notificar execução (não afeta o resultado): {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Cancela na VUUPT toda rota de HOJE sem motorista atribuído até o fim do dia.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--modo-teste", action="store_true",
                        help="Só lista as rotas candidatas, não cancela nada de verdade.")
    args = parser.parse_args()
    main(modo_teste=args.modo_teste)
