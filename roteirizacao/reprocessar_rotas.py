# -*- coding: utf-8 -*-
"""
reprocessar_rotas.py

Exclui rotas ativas no VUUPT (hoje ou futuras, ainda sem motorista
atribuído) e desatribui os pedidos delas (volta pra not_assigned), pra
permitir reprocessamento com os novos parâmetros de nível de
complexidade CPF/CNPJ (regras/complexidade_entrega.py, 10/08).

Este script SÓ EXCLUI. Depois de rodar:
  1. Reenvie os pedidos pelo pipeline normal (a skill nova só é
     recalculada quando o pedido está not_assigned de novo -- ver
     regras/complexidade_entrega.py e vuupt_client.py::
     criar_ou_atualizar_servico).
  2. Rode criar_rotas_diarias.py / incrementar_rotas.py de novo pra
     recriar as rotas com os pedidos já reclassificados.

SEGURANÇA -- uma rota só é candidata a exclusão se TODAS as condições
baterem:
  - status != "canceled" (já cancelada, nada a fazer)
  - start_at (data real, não o nome) é HOJE ou FUTURA
  - agent_id é None (nenhum motorista atribuído -- proxy de "rota
    ainda não iniciada"; rota com motorista atribuído NUNCA é tocada
    por este script, mesmo em modo --confirmar)

cancelar_rota() é chamada sempre com services_action="unassign" --
os pedidos da rota excluída voltam pra not_assigned automaticamente
(não ficam "presos" com status antigo fora de qualquer rota).

Uso:
    py -3.11 roteirizacao/reprocessar_rotas.py                    # dry-run: só lista candidatas
    py -3.11 roteirizacao/reprocessar_rotas.py --uma-rota 12345   # dry-run de UMA rota específica
    py -3.11 roteirizacao/reprocessar_rotas.py --uma-rota 12345 --confirmar   # exclui só essa (validação antes do lote)
    py -3.11 roteirizacao/reprocessar_rotas.py --confirmar        # exclui TODAS as candidatas elegíveis

Sem --confirmar, o script nunca exclui nada -- só mostra o que faria
(mesma convenção de limpar_fingerprints.py), já que cancelar_rota()
nunca foi exercitada contra a API real antes deste script.
"""
import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

_RAIZ_PROJETO = Path(__file__).parent.parent
sys.path.insert(0, str(_RAIZ_PROJETO))
sys.path.insert(0, str(Path(__file__).parent))

import yaml

from rotas_client import listar_rotas, cancelar_rota

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("reprocessar_rotas")

PADRAO_DATA_START_AT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _carregar_config() -> dict:
    with open(_RAIZ_PROJETO / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _data_inicio_rota(rota: dict) -> date | None:
    """Extrai AAAA-MM-DD de start_at. None se ausente/irreconhecível --
    nesse caso a rota NÃO é candidata (não dá pra confirmar que é
    hoje/futura sem essa data)."""
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


def _elegivel(rota: dict) -> tuple[bool, str]:
    """Retorna (elegível, motivo_se_nao)."""
    if rota.get("status") == "canceled":
        return False, "já está cancelada"
    data_inicio = _data_inicio_rota(rota)
    if data_inicio is None:
        return False, "start_at ausente/irreconhecível -- não dá pra confirmar que é hoje/futura"
    if data_inicio < date.today():
        return False, f"start_at ({data_inicio}) é passado"
    if rota.get("agent_id") is not None:
        return False, f"tem motorista atribuído (agent_id={rota.get('agent_id')})"
    return True, ""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--uma-rota", type=int, default=None,
                        help="Restringe a operação a um único route_id (validação antes do lote).")
    parser.add_argument("--confirmar", action="store_true",
                        help="Exclui de verdade (sem isso, só mostra o que seria feito).")
    args = parser.parse_args()

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token", "")
    if not token:
        print("config.yaml sem vuupt_api.token -- abortando.")
        raise SystemExit(1)

    logger.info("Listando rotas ativas no VUUPT...")
    todas_rotas = listar_rotas(token)
    logger.info(f"{len(todas_rotas)} rota(s) encontrada(s) no total.")

    if args.uma_rota is not None:
        candidatas = [r for r in todas_rotas if r.get("id") == args.uma_rota]
        if not candidatas:
            print(f"Rota {args.uma_rota} não encontrada na listagem do VUUPT.")
            raise SystemExit(1)
    else:
        candidatas = todas_rotas

    elegiveis = []
    rejeitadas = []
    for rota in candidatas:
        ok, motivo = _elegivel(rota)
        if ok:
            elegiveis.append(rota)
        else:
            rejeitadas.append((rota, motivo))

    for rota, motivo in rejeitadas:
        logger.info(f"  IGNORADA: rota {rota.get('id')} '{rota.get('name')}' -- {motivo}")

    if not elegiveis:
        print("Nenhuma rota elegível para exclusão.")
        return

    print(f"\n{len(elegiveis)} rota(s) elegível(is) para exclusão (hoje/futura, sem motorista atribuído):")
    for rota in elegiveis:
        print(f"  - id={rota.get('id')} '{rota.get('name')}' start_at={rota.get('start_at')}")

    if not args.confirmar:
        print("\n[DRY-RUN] Nenhuma rota foi excluída. Rode de novo com --confirmar para excluir de verdade.")
        return

    print("\nExcluindo (services_action=unassign -- pedidos voltam pra not_assigned)...")
    sucesso, falha = 0, 0
    for rota in elegiveis:
        route_id = rota.get("id")
        try:
            cancelar_rota(token, route_id, services_action="unassign")
            logger.info(f"  OK: rota {route_id} excluída, pedidos desatribuídos.")
            sucesso += 1
        except Exception as e:
            logger.error(f"  FALHA ao excluir rota {route_id}: {e}")
            falha += 1

    print(f"\n{sucesso} rota(s) excluída(s) com sucesso, {falha} falha(s).")
    if sucesso:
        print("Próximo passo: rodar o pipeline de importação e depois criar_rotas_diarias.py / "
              "incrementar_rotas.py para recriar as rotas com os pedidos reclassificados.")


if __name__ == "__main__":
    main()
