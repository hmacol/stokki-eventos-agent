# -*- coding: utf-8 -*-
"""
reconciliar_pedidos_retirada.py

pipeline.py só barra a importação no VUUPT de pedidos cuja
transportadora resolve para tipo RETIRADA (ex: "CLIENTE RETIRA") NO
MOMENTO da importação -- se a transportadora muda pra RETIRADA
DEPOIS que o serviço já foi criado no VUUPT, nada revalida (achado
27/08, caso #PS-37190: pedido virou "Cliente Retira" no Stokki só
depois do serviço já estar no VUUPT, ninguém cancelou).

Este script fecha esse buraco: audita os serviços 'not_assigned'
recentes no VUUPT, reconsulta a transportadora ATUAL de cada um no
Stokki e cancela os que hoje resolvem pra RETIRADA no catálogo
BD_TRANSPORTADORAS.xlsx. Só cancela quando o serviço ainda está
'not_assigned' (sem motorista/rota) -- qualquer outro status vira
aviso de intervenção manual, nunca cancelamento automático.

Complementa (não substitui) a checagem já feita em
painel_agentes/pedidos_parados_triagem.py:notificar_cliente_retira,
que cancela na hora quando alguém classifica o pedido como "Cliente
Retira" na triagem -- este script cobre o caso de a transportadora
ser trocada direto no Stokki, sem passar pela triagem.
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
from stokki.auth import StokkiSession
from stokki import pedidos as stokki_pedidos
from regras.transportadoras import CatalogoTransportadoras
import tratativas
from notificar_execucao_agente import notificar_execucao

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_RAIZ / "dados" / "reconciliar_retirada.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("reconciliar_retirada")

TRANSPORTADORAS = _RAIZ / "dados" / "BD_TRANSPORTADORAS.xlsx"


def _carregar_config() -> dict:
    with open(_RAIZ / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _extrair_id_stokki(code: str) -> int | None:
    """PS-XXXXX / #PS-XXXXX -> XXXXX (int). O código PS é sempre o ID
    interno do Stokki, ver stokki/pedidos.py:extrair_codigo_ps_da_linha."""
    m = re.search(r"PS-(\d+)", code or "", re.IGNORECASE)
    return int(m.group(1)) if m else None


def main(modo_teste: bool = True, cancelar: bool = False, dias: int = 10):
    inicio = time.monotonic()
    logger.info("=" * 60)
    logger.info(f"{'[MODO TESTE] ' if modo_teste else ''}Reconciliação Retirada VUUPT (últimos {dias} dias)")
    logger.info("=" * 60)

    config = _carregar_config()
    token = config.get("vuupt_api", {}).get("token")
    if not token:
        logger.error("Token da API VUUPT não encontrado no config.yaml.")
        return

    client = VuuptClient(token)
    dt_inicio = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")

    filtros = [
        {"field": "status", "operator": "eq", "value": "not_assigned"},
        {"field": "created_at", "operator": "gte", "value": dt_inicio},
    ]
    servicos = client.listar_servicos(filtros)
    candidatos = [s for s in servicos if _extrair_id_stokki(s.get("code", "")) is not None]
    logger.info(f"{len(candidatos)} serviço(s) not_assigned com código PS válido (de {len(servicos)} total).")

    resumo_etapas = {}
    if not candidatos:
        duracao = time.monotonic() - inicio
        resumo_etapas["Reconciliação Retirada VUUPT"] = {"status": "ok", "detalhe": "nenhum serviço not_assigned no período"}
        notificar_execucao(resumo_etapas, duracao, modo_teste, config)
        return

    catalogo = CatalogoTransportadoras.carregar(TRANSPORTADORAS)
    sess_stokki = StokkiSession(config)

    cancelados = []
    avisos = []

    for s in candidatos:
        code = s.get("code", "")
        id_stokki = _extrair_id_stokki(code)
        try:
            detalhe = stokki_pedidos.obter_detalhe(sess_stokki, id_stokki)
        except Exception as e:
            logger.warning(f"{code}: falha ao consultar detalhe no Stokki -- {e}")
            avisos.append(f"{code}: falha ao consultar Stokki ({e})")
            continue

        transp_bloco = detalhe.get("transportadora") or {}
        nome_transp = transp_bloco.get("nome", "")
        if not nome_transp:
            continue

        cnpj_transp = "".join(c for c in (transp_bloco.get("documento") or "") if c.isdigit())
        resultado = catalogo.resolver(nome_transp, cnpj=cnpj_transp)
        if resultado.tipo != "RETIRADA":
            continue

        logger.warning(
            f"{code}: transportadora no Stokki agora é '{nome_transp}' (RETIRADA), "
            f"mas o serviço {s['id']} segue not_assigned no VUUPT."
        )

        if modo_teste:
            logger.info(f"  [MODO TESTE] Seria cancelado o serviço ID {s['id']}.")
            avisos.append(f"{code}: seria cancelado (modo teste) -- transportadora agora é '{nome_transp}'")
            continue

        if not cancelar:
            logger.info(f"  [NÃO CANCELADO] rode com --cancelar para efetivar.")
            avisos.append(f"{code}: pendente de cancelamento manual -- transportadora agora é '{nome_transp}'")
            continue

        try:
            client.cancelar_servico(s["id"])
            logger.info(f"  [CANCELADO] serviço ID {s['id']} ({code}).")
            cancelados.append(code)
            tratativas.registrar_evento(
                code.lstrip("#"), "RECONCILIACAO_RETIRADA", "SERVICO_VUUPT_CANCELADO_RETIRADA",
                service_id=s["id"],
                texto=(
                    f"Serviço {s['id']} cancelado -- transportadora no Stokki é "
                    f"'{nome_transp}' (RETIRADA), mas o pedido já tinha sido importado "
                    f"no VUUPT antes dessa mudança."
                ),
            )
        except VuuptAPIError as e:
            logger.error(f"  [ERRO AO CANCELAR] {code}: {e}")
            avisos.append(f"{code}: erro ao cancelar -- {e}")

    logger.info("=" * 60)
    logger.info(f"Reconciliação concluída. Cancelamentos: {len(cancelados)}. Avisos: {len(avisos)}.")
    logger.info("=" * 60)

    duracao = time.monotonic() - inicio
    detalhe = f"{len(candidatos)} serviço(s) not_assigned verificado(s), {len(cancelados)} cancelado(s)"
    if avisos:
        detalhe += f", {len(avisos)} aviso(s)"
    resumo_etapas["Reconciliação Retirada VUUPT"] = {"status": "ok", "detalhe": detalhe}
    notificar_execucao(resumo_etapas, duracao, modo_teste, config)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cancela no VUUPT pedidos que viraram RETIRADA no Stokki depois de já importados")
    parser.add_argument("--modo-teste", action="store_true", help="Apenas simula, sem cancelar nada")
    parser.add_argument("--cancelar", action="store_true", help="Efetua o cancelamento dos serviços encontrados")
    parser.add_argument("--dias", type=int, default=10, help="Janela (em dias) de created_at pra buscar serviços not_assigned")
    args = parser.parse_args()

    modo_teste = args.modo_teste or (not args.cancelar)
    main(modo_teste=modo_teste, cancelar=args.cancelar, dias=args.dias)