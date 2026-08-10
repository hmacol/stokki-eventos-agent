# -*- coding: utf-8 -*-
"""
verificar_pedidos_duplicados_vuupt.py

Audita os serviços recentes no VUUPT em busca de duplicidades (mesmo
pedido re-importado com pequenas variações no 'code') e, quando
configurado, cancela as cópias sobressalentes que ainda estejam
'not_assigned' -- nunca mexe em serviços já atribuídos/concluídos.
"""
import argparse
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import yaml

_RAIZ = Path(__file__).parent
sys.path.insert(0, str(_RAIZ))

from vuupt_client import VuuptClient, VuuptAPIError
from notificar_execucao_agente import notificar_execucao

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "verificar_duplicados.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("verificar_duplicados")

# Prioridade de manutenção -- quanto menor, mais "protegido" contra
# cancelamento. Só 'not_assigned' pode ser cancelado automaticamente.
# 'done'/'accepted' confirmados em produção (09/08) como os status reais
# retornados pela API além de 'not_assigned'/'canceled'; 'completed',
# 'in_route' e 'assigned' mantidos por segurança caso apareçam também.
# Qualquer status não mapeado aqui é tratado como PROTEGIDO por padrão
# (rank -1, antes até de 'completed') -- a segurança de fato contra
# cancelamento vem da checagem explícita de status == 'not_assigned' no
# loop principal, esta ordem só decide qual instância é exibida como
# "mantida" no log/relatório.
ORDEM_STATUS = {
    "completed": 0, "done": 0,
    "in_route": 1,
    "assigned": 2, "accepted": 2,
    "not_assigned": 3,
}


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def normalizar_codigo(code: str) -> str:
    """Remove espaços, '#' inicial, sufixos de re-tentativa/reenvio
    (-R1, _R2, -DUP...) e uppercasa, para chegar no código raiz do
    pedido.

    NÃO remove números genéricos no final (ex: o "-36147" de
    "PS-36147") -- nesse formato de código o número FAZ PARTE do
    pedido, não é um sufixo de duplicidade. Só sufixos explícitos de
    reenvio (R + dígitos, ou DUP) são considerados marcadores de
    duplicidade."""
    if not code:
        return ""
    code = code.strip().lstrip("#").upper()
    code = re.sub(r"(?:[_\-](?:R\d+|DUP))+$", "", code)
    return code


def analisar_duplicados(servicos: list[dict]) -> dict:
    agrupados: dict[str, list[dict]] = {}
    for s in servicos:
        if s.get("status") == "canceled":
            continue
        c_norm = normalizar_codigo(s.get("code", ""))
        if not c_norm:
            continue
        agrupados.setdefault(c_norm, []).append(s)

    return {k: v for k, v in agrupados.items() if len(v) > 1}


def main(modo_teste: bool = True, cancelar_duplicados: bool = False, dias: int = 7):
    inicio = time.monotonic()
    logger.info("=" * 60)
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Iniciando verificação de duplicados no VUUPT (últimos {dias} dias)")
    logger.info("=" * 60)

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token")
    if not token:
        logger.error("Token da API VUUPT não encontrado no config.yaml.")
        return

    client = VuuptClient(token)
    dt_inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")

    filtros = [{"field": "created_at", "operator": "gte", "value": dt_inicio}]

    logger.info(f"Buscando serviços criados a partir de {dt_inicio}...")
    todos_servicos = client.listar_servicos(filtros)
    logger.info(f"Total de serviços obtidos: {len(todos_servicos)}")

    grupos_duplicados = analisar_duplicados(todos_servicos)
    logger.info(f"Grupos de duplicados identificados: {len(grupos_duplicados)}")

    cancelados_count = 0
    avisos_manuais = []

    for code_raiz, lista in grupos_duplicados.items():
        logger.info(f"--- Pedido Duplicado: {code_raiz} ({len(lista)} instâncias) ---")

        # Prioridade: completed > in_route > assigned > not_assigned;
        # empate no mesmo status = mantém o mais recente.
        lista_ordenada = sorted(
            lista,
            key=lambda x: (ORDEM_STATUS.get(x.get("status"), -1), x.get("created_at", "")),
        )
        # Dentro do grupo de menor ordem_status, o mais recente vem por
        # último no sort acima (created_at crescente) -- reordena esse
        # subgrupo para ficar primeiro.
        menor_ordem = ORDEM_STATUS.get(lista_ordenada[0].get("status"), -1)
        subgrupo_topo = [s for s in lista_ordenada if ORDEM_STATUS.get(s.get("status"), -1) == menor_ordem]
        subgrupo_topo.sort(key=lambda x: x.get("created_at", ""), reverse=True)
        resto = [s for s in lista_ordenada if ORDEM_STATUS.get(s.get("status"), -1) != menor_ordem]
        lista_ordenada = subgrupo_topo + resto

        manter = lista_ordenada[0]
        sobressalentes = lista_ordenada[1:]

        logger.info(f"  [MANTER] ID {manter.get('id')} - Code: {manter.get('code')} - Status: {manter.get('status')}")

        for sob in sobressalentes:
            st = sob.get("status")
            s_id = sob.get("id")
            s_code = sob.get("code")

            if st != "not_assigned":
                aviso = f"Pedido '{code_raiz}': serviço ID {s_id} (Code: {s_code}) em status '{st}' não pode ser cancelado automaticamente -- intervenção manual necessária."
                logger.warning(f"  [ATENÇÃO] {aviso}")
                avisos_manuais.append(aviso)
                continue

            if modo_teste:
                logger.info(f"  [MODO TESTE] Seria cancelado o serviço ID {s_id} (Code: {s_code})")
            elif cancelar_duplicados:
                try:
                    client.cancelar_servico(s_id)
                    logger.info(f"  [CANCELADO] ID {s_id} - Code: {s_code}")
                    cancelados_count += 1
                except VuuptAPIError as e:
                    logger.error(f"  [ERRO AO CANCELAR] ID {s_id}: {e}")
            else:
                logger.info(f"  [NÃO CANCELADO] ID {s_id} - Code: {s_code} -- rode com --cancelar-duplicados para efetivar.")

    logger.info("=" * 60)
    logger.info(f"Verificação concluída. Duplicados encontrados: {len(grupos_duplicados)} grupos. Cancelamentos efetuados: {cancelados_count}")
    logger.info("=" * 60)

    duracao = time.monotonic() - inicio
    detalhe = (
        f"{len(todos_servicos)} serviços auditados, {len(grupos_duplicados)} grupo(s) duplicado(s), "
        f"{cancelados_count} cancelado(s)"
    )
    if avisos_manuais:
        detalhe += f", {len(avisos_manuais)} aviso(s) de intervenção manual"

    resumo_etapas = {
        "Verificação de Duplicados VUUPT": {"status": "ok", "detalhe": detalhe},
    }
    notificar_execucao(resumo_etapas, duracao, modo_teste, config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verifica pedidos duplicados no VUUPT")
    parser.add_argument("--modo-teste", action="store_true", help="Apenas simula a auditoria sem alterar dados no VUUPT")
    parser.add_argument("--cancelar-duplicados", action="store_true", help="Efetua o cancelamento de cópias not_assigned sobressalentes")
    parser.add_argument("--dias", type=int, default=7, help="Número de dias para retroagir a busca de serviços")
    args = parser.parse_args()

    modo_teste = args.modo_teste or (not args.cancelar_duplicados)
    main(modo_teste=modo_teste, cancelar_duplicados=args.cancelar_duplicados, dias=args.dias)
